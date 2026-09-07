"""Session lifecycle (plan §7).

Sessions bind to an agent_id. History is read from the LangGraph checkpointer
(thread_id keyed), not the business table.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from langchain_core.messages import RemoveMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.models.schemas import KbRef, UploadOut
from app.services.thinking_patch import extract_reasoning

logger = logging.getLogger(__name__)

# deepagents 摘要中间件卸载全量历史的虚拟目录前缀（StateBackend files 通道内）。
# 撤回时该前缀下的所有键（thread md + media）一并失效，防止已撤回内容经
# read_file 回读 / 摘要文本复活（"摘要投毒"根因，见 withdraw_turn）。
_CONVERSATION_HISTORY_PREFIX = "/conversation_history/"


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
        # KB 徽标回显：kb_ids 同源（additional_kwargs），批量解析为 {id, name}。
        kbs_by_idx = await self._resolve_kbs(messages)
        return [
            _serialize_message(msg, attachments_by_idx.get(i), kbs_by_idx.get(i))
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

    async def _resolve_kbs(self, messages: list) -> dict[int, list[KbRef]]:
        """Map each human message's kb_ids → resolved KbRef list (batch, no N+1).

        kb_ids live on HumanMessage.additional_kwargs (written at send time).
        KBs are workspace-global (no per-session scoping needed, unlike
        uploads). Deleted KBs simply drop out of the resolved mapping — the
        badge degrades to "gone" instead of erroring, mirroring the stale-
        upload semantics of ``_resolve_attachments``.
        """
        per_msg: dict[int, list[str]] = {}
        all_ids: list[uuid.UUID] = []
        for i, m in enumerate(messages):
            if getattr(m, "type", None) != "human":
                continue
            ak = getattr(m, "additional_kwargs", None) or {}
            raw = ak.get("kb_ids")
            if not isinstance(raw, list) or not raw:
                continue
            clean: list[str] = []
            for kid in raw:
                if not isinstance(kid, str):
                    continue
                try:
                    parsed = uuid.UUID(kid)
                except (ValueError, AttributeError, TypeError):
                    continue
                clean.append(kid)
                all_ids.append(parsed)
            if clean:
                per_msg[i] = clean

        if not all_ids:
            return {}
        rows = await dbmod.get_kbs_by_ids(self.pool, all_ids)
        by_id = {str(r["id"]): r for r in rows}
        out: dict[int, list[KbRef]] = {}
        for i, ids in per_msg.items():
            seq = [KbRef(id=by_id[k]["id"], name=by_id[k]["name"]) for k in ids if k in by_id]
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
        ``RemoveMessage(id)`` deltas through ``agent.aupdate_state``. The original
        writes remain on disk (shadow) but replay (``_load_messages`` / graph
        state) no longer returns them — both /history and the next LLM context
        are correct automatically.

        Besides the messages channel, two summarization side-channels must be
        invalidated together, or withdrawn content re-enters the LLM context
        through them ("摘要投毒"):
        - ``_summarization_event`` (summary_message + cutoff_index): the event
          survives a messages-only removal, and its absolute cutoff_index is
          shifted by it, swallowing NEWER messages (the user's follow-up
          instructions) into the summarized void.
        - ``files`` keys under ``/conversation_history/``: the offloaded full
          history (incl. withdrawn user queries) that the agent re-reads via
          read_file.
        Clearing both makes the next model call see the cleaned raw messages
        only; if the context again exceeds the threshold, summarization
        re-fires lazily and rebuilds both artifacts from post-withdraw
        messages (self-healing).

        A thread abandoned mid-run (cancelled stream / pending HITL) carries
        pending tasks (``state.next``); after the update they are cleared with
        ``values=None, as_node=END`` — the withdrawn turn's in-flight work dies
        with it, and the clear path bypasses the writer chain that breaks on
        such states (KeyError 'model' → HTTP 500).

        Returns the withdrawn user message's raw text + attachments + selected
        KBs so the front-end can prefill the composer for edit-and-resend.
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
        await self._apply_withdraw_update(agent, config, ids_to_remove)

        # 5) recover raw_content + attachments + kbs for composer prefill
        ak = getattr(target, "additional_kwargs", None) or {}
        raw = ak.get("raw_content")
        if not (isinstance(raw, str) and raw):
            c = getattr(target, "content", "")
            raw = c if isinstance(c, str) else str(c)

        attachments = await self._resolve_human_attachments(ak.get("upload_ids"), session_id)
        kbs = await self._resolve_human_kbs(ak.get("kb_ids"))
        return {
            "raw_content": raw,
            "attachments": attachments,
            "kbs": kbs,
            "removed_ids": ids_to_remove,
        }

    async def _apply_withdraw_update(
        self,
        agent: CompiledStateGraph,
        config: dict,
        ids_to_remove: list[str],
    ) -> None:
        """Apply the withdraw state update: messages soft-delete + summarization
        side-channel invalidation + stale pending-task cleanup.

        Update routing (all through the public aupdate_state API, reducers apply
        per channel — files 的 {path: None} 增量即删除键):
        - 摘要通道仅在图中存在且确有产物时写入（未触发过摘要的线程保持
          messages-only 更新，行为与历史版本一致）。
        - 常规更新走无 as_node 推断路径（干净回合边界，生产验证）；中止态
          （cancelled 流残留 pending 任务）下该路径经 model 节点 writer 链
          会触发 langgraph 分支 ``KeyError: 'model'``（撤回 500 的根因），
          回退 ``as_node='tools'``（同一增量经 tools writer 链可正常应用，
          已在真实中止态复现验证）。
        - 更新成功后若 ``state.next`` 非空（中止残留 / HITL 待审批 / tools
          回退触发的路由），以 ``values=None, as_node=END`` 清空 pending 任务：
          被撤回回合的未竟执行应一并作废，且清尾路径不经过 writer 链，
          任何状态下均可安全执行。
        """
        state = await agent.aget_state(config)
        update: dict[str, Any] = {
            "messages": [RemoveMessage(id=i) for i in ids_to_remove]
        }
        channels = getattr(agent, "channels", None) or {}
        if (
            "_summarization_event" in channels
            and state.values.get("_summarization_event") is not None
        ):
            update["_summarization_event"] = None
        if "files" in channels:
            stale = [
                path
                for path in (state.values.get("files") or {})
                if path.startswith(_CONVERSATION_HISTORY_PREFIX)
            ]
            if stale:
                update["files"] = {path: None for path in stale}

        try:
            await agent.aupdate_state(config, update)
        except Exception:
            nodes = getattr(agent, "nodes", None) or {}
            fallback = next(
                (name for name in ("tools",) if name in nodes),
                next((name for name in nodes if name != "__start__"), None),
            )
            if fallback is None:
                raise
            logger.exception(
                "withdraw: 常规 aupdate_state 失败（中止态 writer 链已知问题），"
                "回退 as_node=%s 重试",
                fallback,
            )
            await agent.aupdate_state(config, update, as_node=fallback)

        state_after = await agent.aget_state(config)
        if state_after.next:
            await agent.aupdate_state(config, None, as_node=END)

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

    async def _resolve_human_kbs(self, kb_ids: object) -> list[KbRef]:
        """Resolve a single message's kb_ids → ordered, de-duplicated KbRef list.

        Mirrors ``_resolve_human_attachments`` for knowledge bases; stale /
        non-UUID ids are skipped silently (deleted KBs drop out of prefill).
        """
        if not isinstance(kb_ids, list) or not kb_ids:
            return []
        parsed: list[uuid.UUID] = []
        seen: set[str] = set()
        for kid in kb_ids:
            if not isinstance(kid, str) or kid in seen:
                continue
            try:
                parsed.append(uuid.UUID(kid))
            except (ValueError, TypeError, AttributeError):
                continue
            seen.add(kid)
        if not parsed:
            return []
        rows = await dbmod.get_kbs_by_ids(self.pool, parsed)
        by_id = {str(r["id"]): r for r in rows}
        return [
            KbRef(id=by_id[str(p)]["id"], name=by_id[str(p)]["name"])
            for p in parsed
            if str(p) in by_id
        ]

    @staticmethod
    async def _load_messages(checkpointer: AsyncPostgresSaver, config: dict) -> list:
        """Resolve the ``messages`` channel for a thread (delta-aware)."""
        # 1) Preferred path: delta channel history (langgraph >= 1.2).
        #
        #    返回结构 entry = {writes, seed}：
        #    - ``seed``：最近一次带版本的完整快照（delta 增量的基线，可能为 None——
        #      线程尚无版本化 checkpoint 时全量都在 writes 里）；
        #    ``writes``：seed 之后的增量（含 RemoveMessage 软删）。
        #    【关键】必须以 seed.value 起步再叠加 writes——若从空表起步，只能得到
        #    最后一个版本化 checkpoint 之后的增量，历史被截断为最后几轮
        #    （实测 69 条消息只回出 14 条 = 恰好最后两轮）。
        #    【关键】entry 一旦存在即以 seed(+writes) 为唯一真值，不得以
        #    ``if writes:`` 门控：流式推理刚结束的线程，walk 停在最新版本化
        #    快照上，writes 恒为空而 seed 已是全量（实测 writes=0 + seed=55）。
        #    空 writes 若把合法 seed 丢弃并落回下方 blob 路径，langgraph>=1.2
        #    的 channel_values["messages"] 是 _DeltaSnapshot 占位对象，
        #    ``list(...)`` 迭代出垃圾条目（无 type/content），/history 只回一条
        #    空消息 → 前端历史重灌后全部文本消失（seed-only 竞态根因）。
        if hasattr(checkpointer, "aget_delta_channel_history"):
            delta = await checkpointer.aget_delta_channel_history(
                config=config, channels=["messages"]
            )
            entry = (delta or {}).get("messages")
            writes = None
            seed = None
            if entry is not None:
                writes = getattr(entry, "writes", None)
                seed = getattr(entry, "seed", None)
                if isinstance(entry, dict):
                    if writes is None:
                        writes = entry.get("writes")
                    if seed is None:
                        seed = entry.get("seed")
                from langgraph.graph.message import add_messages

                # seed 兼容 _DeltaSnapshot(.value) / 直接值 / None 三种形态
                seed_value = getattr(seed, "value", seed)
                messages = list(seed_value) if seed_value else []
                for w in writes or []:
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


def _serialize_message(
    msg,
    attachments: list[UploadOut] | None = None,
    kbs: list[KbRef] | None = None,
) -> dict:
    """Best-effort message -> dict for /sessions/{id}/history."""
    role = getattr(msg, "type", type(msg).__name__)
    content = getattr(msg, "content", "")
    # 展示优先用用户原始输入：附件/KB 消息的 content 被 ContentComposer 注入了
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
    # 思考内容回显（仅 ai 消息携带，GLM/DeepSeek reasoning_content 随 checkpoint
    # 持久化）：前端历史回放据此重建"思考过程"折叠块，与实时流 ThinkingBlock
    # 同构。仅序列化展示层——_load_messages 返回的原始消息对象同时服务回撤定位
    # （RemoveMessage）与图状态（摘要中间件的上下文重建），不得为展示而改写。
    reasoning = extract_reasoning(msg)
    if reasoning:
        out["reasoning"] = reasoning
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
    if kbs:
        out["kbs"] = kbs
    return out
