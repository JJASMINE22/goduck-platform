"""Session lifecycle (plan §7).

Sessions bind to an agent_id. History is read from the LangGraph checkpointer
(thread_id keyed), not the business table.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from langchain_core.messages import RemoveMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.models.schemas import UploadOut

logger = logging.getLogger(__name__)


class SessionService:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def create(
        self,
        user_id: str,
        agent_id: uuid.UUID,
        title: Optional[str],
        thread_id: Optional[str] = None,
    ) -> dict:
        """创建会话（同用户活跃会话内标题唯一；归档会话豁免，可复用标题）。

        - 标题归一：strip 后为空视为未命名（NULL，允许多个）。
        - 自传 thread_id：撞库即 400（thread_id 是 checkpointer 键，全局唯一）。
        - 预检（语义化 409/400）+ DB 部分唯一索引竞态兜底（见 db.create_session）。
        """
        if await dbmod.get_agent(self.pool, agent_id) is None:
            raise KeyError(f"agent {agent_id} not found")
        title = (title or "").strip() or None
        if title is not None:
            dup = await dbmod.get_active_session_by_user_title(self.pool, user_id, title)
            if dup is not None:
                raise FileExistsError(title)  # router maps -> 409
        if not thread_id:
            thread_id = f"thread-{uuid.uuid4().hex}"
        elif await dbmod.get_session_by_thread(self.pool, thread_id) is not None:
            raise ValueError(f"thread_id '{thread_id}' 已被其它会话使用")  # -> 400
        return await dbmod.create_session(
            self.pool,
            agent_id=agent_id,
            user_id=user_id,
            thread_id=thread_id,
            title=title,
        )

    async def list_by_user(self, user_id: str, include_archived: bool = False) -> list[dict]:
        return await dbmod.list_sessions_by_user(self.pool, user_id, include_archived=include_archived)

    async def get(self, session_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.get_session(self.pool, session_id)

    async def archive(self, session_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.archive_session(self.pool, session_id)

    async def delete(self, session_id: uuid.UUID) -> bool:
        """Hard delete (physical) + checkpoint cleanup. Irreversible."""
        return await dbmod.delete_session(self.pool, session_id)

    async def touch(self, session_id: uuid.UUID) -> None:
        await dbmod.touch_session(self.pool, session_id)

    async def get_history(
        self,
        checkpointer: AsyncPostgresSaver,
        thread_id: str,
        session_id: uuid.UUID,
    ) -> list[dict]:
        """Read full conversation history from checkpointer (thread_id keyed).

        langgraph >= 1.2 persists ``add_messages`` channels (i.e. ``messages``)
        as incremental writes in ``checkpoint_writes`` and reconstructs them via
        *delta channel history* — they are NO LONGER stored as blobs. Therefore
        ``aget_tuple`` (blob-only resolution) returns an empty ``messages``
        channel. We mirror what the compiled graph's ``aget_state`` does:
        replay the message deltas through the ``add_messages`` reducer.
        """
        config = {"configurable": {"thread_id": thread_id}}
        try:
            messages = await self._load_messages(checkpointer, config)
        except Exception:
            logger.exception("get_history failed for thread %s", thread_id)
            return []

        # 附件缩略回显：upload_ids 记录在 human 消息 additional_kwargs（发送时写入），
        # 这里收集后一次批量查库注入，避免逐消息 N+1。与 content 完全解耦。
        attachments_by_idx = await self._resolve_attachments(messages, session_id)
        return [
            _serialize_message(msg, attachments_by_idx.get(i))
            for i, msg in enumerate(messages)
        ]

    async def _resolve_attachments(
        self,
        messages: list,
        session_id: uuid.UUID,
    ) -> dict[int, list[UploadOut]]:
        """Map each human message's upload_ids → resolved UploadOut list.

        upload_ids live on HumanMessage.additional_kwargs (written at send time,
        never parsed out of content). Invalid / non-UUID ids are skipped; a
        session_id scope guard is enforced inside get_uploads_by_ids so a stale
        id from another session can never leak.
        """
        per_msg: dict[int, list[str]] = {}
        all_ids: list[uuid.UUID] = []
        for i, m in enumerate(messages):
            if getattr(m, "type", None) != "human":
                continue
            ak = getattr(m, "additional_kwargs", None) or {}
            raw = ak.get("upload_ids")
            if not isinstance(raw, list) or not raw:
                continue
            clean: list[str] = []
            for uid in raw:
                if not isinstance(uid, str):
                    continue
                try:
                    parsed = uuid.UUID(uid)
                except (ValueError, AttributeError, TypeError):
                    continue
                clean.append(uid)
                all_ids.append(parsed)
            if clean:
                per_msg[i] = clean

        if not all_ids:
            return {}
        rows = await dbmod.get_uploads_by_ids(self.pool, all_ids, session_id)
        out: dict[int, list[UploadOut]] = {}
        for i, ids in per_msg.items():
            seq = [_upload_row_to_out(rows[uid]) for uid in ids if uid in rows]
            if seq:
                out[i] = seq
        return out

    async def withdraw_turn(
        self,
        checkpointer: AsyncPostgresSaver,
        agent: CompiledStateGraph,
        thread_id: str,
        session_id: uuid.UUID,
        message_id: str,
    ) -> dict:
        """Withdraw a full conversation turn anchored on a user message.

        A turn = the HumanMessage with ``id == message_id`` plus every message
        after it up to (but not including) the next HumanMessage. Removing the
        whole contiguous block keeps tool_call ↔ ToolMessage pairs intact so
        the LLM context stays well-formed (no dangling tool_call).

        Removal is a *soft delete* via the ``add_messages`` reducer: we append
        ``RemoveMessage(id)`` deltas through ``agent.update_state``. The original
        writes remain on disk (shadow) but replay (``_load_messages`` / graph
        state) no longer returns them — both /history and the next LLM context
        are correct automatically.

        Returns the withdrawn user message's raw text + attachments so the
        front-end can prefill the composer for edit-and-resend.
        """
        config = {"configurable": {"thread_id": thread_id}}
        messages = await self._load_messages(checkpointer, config)

        # 1) locate the anchor human message
        start = -1
        target = None
        for i, m in enumerate(messages):
            if getattr(m, "id", None) == message_id:
                start = i
                target = m
                break
        if target is None:
            raise ValueError(f"消息 {message_id} 不存在")
        if getattr(target, "type", None) != "human":
            raise ValueError("只能回撤用户消息所在的回合")

        # 2) contiguous block [start, next_human) — guarantees tool pair integrity
        end = start + 1
        while end < len(messages) and getattr(messages[end], "type", None) != "human":
            end += 1
        block = messages[start:end]

        # 3) collect ids; messages without an id cannot be soft-deleted (logged)
        ids_to_remove: list[str] = []
        for m in block:
            mid = getattr(m, "id", None)
            if mid:
                ids_to_remove.append(str(mid))
            else:
                logger.warning(
                    "withdraw: 回合内消息缺 id，跳过软删 (type=%s)",
                    getattr(m, "type", "?"),
                )
        if not ids_to_remove:
            raise ValueError("该回合无可删除的消息（缺少消息 id）")

        # 4) soft delete via the add_messages reducer (no raw checkpoint writes).
        #    必须用 aupdate_state：checkpointer 是 AsyncPostgresSaver（仅异步方法），
        #    调同步 update_state 会去用不存在的 put() → 500。
        await agent.aupdate_state(
            config,
            {"messages": [RemoveMessage(id=i) for i in ids_to_remove]},
        )

        # 5) recover raw_content + attachments for composer prefill
        ak = getattr(target, "additional_kwargs", None) or {}
        raw = ak.get("raw_content")
        if not (isinstance(raw, str) and raw):
            c = getattr(target, "content", "")
            raw = c if isinstance(c, str) else str(c)

        attachments = await self._resolve_human_attachments(ak.get("upload_ids"), session_id)
        return {
            "raw_content": raw,
            "attachments": attachments,
            "removed_ids": ids_to_remove,
        }

    async def _resolve_human_attachments(
        self,
        upload_ids: object,
        session_id: uuid.UUID,
    ) -> list[UploadOut]:
        """Resolve a human message's upload_ids → ordered UploadOut list.

        Mirrors ``_resolve_attachments`` but for a single message's id list,
        preserving order and de-duplicating. Stale / non-UUID ids are skipped;
        ``get_uploads_by_ids`` hard-scopes to ``session_id`` so a foreign id
        never leaks.
        """
        if not isinstance(upload_ids, list) or not upload_ids:
            return []
        parsed: list[uuid.UUID] = []
        seen: set[str] = set()
        for uid in upload_ids:
            if not isinstance(uid, str) or uid in seen:
                continue
            try:
                parsed.append(uuid.UUID(uid))
            except (ValueError, TypeError, AttributeError):
                continue
            seen.add(uid)
        if not parsed:
            return []
        rows = await dbmod.get_uploads_by_ids(self.pool, parsed, session_id)
        out: list[UploadOut] = []
        for p in parsed:
            row = rows.get(str(p))
            if row:
                out.append(_upload_row_to_out(row))
        return out

    @staticmethod
    async def _load_messages(checkpointer: AsyncPostgresSaver, config: dict) -> list:
        """Resolve the ``messages`` channel for a thread (delta-aware)."""
        # 1) Preferred path: delta channel history (langgraph >= 1.2).
        if hasattr(checkpointer, "aget_delta_channel_history"):
            delta = await checkpointer.aget_delta_channel_history(
                config=config, channels=["messages"]
            )
            entry = (delta or {}).get("messages")
            writes = None
            if entry is not None:
                writes = getattr(entry, "writes", None)
                if writes is None and isinstance(entry, dict):
                    writes = entry.get("writes")
            if writes:
                from langgraph.graph.message import add_messages

                messages = []
                for w in writes:
                    # each delta is (checkpoint_id, channel, value); value is the
                    # message list written in that superstep (incl. RemoveMessage).
                    value = w[-1] if isinstance(w, (tuple, list)) else w
                    messages = add_messages(messages, value)
                return messages

        # 2) Legacy fallback: blob resolution (older langgraph / non-delta channel).
        snapshot = await checkpointer.aget_tuple(config)
        if snapshot is None:
            return []
        state = snapshot.checkpoint.get("channel_values") if snapshot.checkpoint else None
        if state and "messages" in state:
            return list(state["messages"])
        return []


def _upload_row_to_out(row: dict) -> UploadOut:
    """DB upload row → UploadOut (for history attachment rendering)."""
    return UploadOut(
        upload_id=row["id"],
        kind=row["kind"],
        filename=row["filename"],
        mime=row["mime"],
        size=row["size"],
        page_count=row.get("page_count"),
        preview=row.get("preview"),
    )


def _serialize_message(msg, attachments: list[UploadOut] | None = None) -> dict:
    """Best-effort message -> dict for /sessions/{id}/history."""
    role = getattr(msg, "type", type(msg).__name__)
    content = getattr(msg, "content", "")
    # 展示优先用用户原始输入：附件消息的 content 被 ContentComposer 注入了
    # 给模型看的 anchor 表格，raw_content 保留了用户真正发送的文本。
    ak = getattr(msg, "additional_kwargs", None) or {}
    raw = ak.get("raw_content")
    display = raw if isinstance(raw, str) and raw else content
    out = {
        # LangGraph 真实消息 id：撤回（withdraw）以此为 RemoveMessage 定位键。
        # 历史接口必须回传，否则前端只能合成 id，无法精确撤回。
        "id": getattr(msg, "id", None),
        "role": role,
        "name": getattr(msg, "name", None),
        "content": display if isinstance(display, str) else str(display),
    }
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = [
            {"name": tc.get("name"), "args": tc.get("args"), "id": tc.get("id")}
            for tc in tool_calls
        ]
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    if attachments:
        out["attachments"] = attachments
    return out
