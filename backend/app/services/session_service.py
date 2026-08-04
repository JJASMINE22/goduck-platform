"""Session lifecycle (plan §7).

Sessions bind to an agent_id. History is read from the LangGraph checkpointer
(thread_id keyed), not the business table.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
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
        if await dbmod.get_agent(self.pool, agent_id) is None:
            raise KeyError(f"agent {agent_id} not found")
        if not thread_id:
            thread_id = f"thread-{uuid.uuid4().hex}"
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
