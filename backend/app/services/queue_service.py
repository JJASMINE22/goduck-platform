"""会话排队发送服务（plan: queued-send-messages）。

候客厅语义：直发路径不落 queue 表，消息真相源恒为 checkpointer 历史；
排水（drain）复用与手工直发完全相同的发送管道（同一 factory、同一门禁、
同一 last_agent_id 写点），排队消息与普通回合逐字节同构，下游零特判。

并发模型（单 worker）：
- per-session asyncio 锁串行化排水的 claim→start_run 段与 withdraw / cancel
  的整段操作，封闭 check-then-act 窗口（多副本部署由 FOR UPDATE SKIP LOCKED
  兜底认领互斥）；
- on_final 内的续排一律 create_task（spawn_drain）：drain 绝不阻塞 run task
  收尾——cancel 持锁 join 的场景下，awaited drain 会把 join 拖到超时；
- done → drain → done 自推进与订阅者无关（标签页关闭照样逐条跑完）。

状态机（项级 pending|sending|sent|failed|canceled + 会话级暂停态）见
.kilo/plans/queued-send-messages.md §2。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from typing import Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.deps import SimpleUser
from app.models import db as dbmod
from app.models.schemas import Attachment
from app.services.message_content import (
    MessageContentError,
    validate_message_content,
)
from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)


class QueueLimitError(Exception):
    """待发队列已达上限（pending+sending 计数）。"""


class _RecheckFailure(Exception):
    """发送前复检失败：单条意图失效 → 行置 failed 跳过续排（不暂停队列）。"""


# _send_claimed 的三态结局
_STARTED = "started"  # run 已启动，递推交新 run 的 on_final
_FAILED = "failed"  # 复检失败，继续消化下一条
_STOP = "stop"  # start_run 冲突/限额：行已回置 pending，本轮排水中止


def _parse_ids(raw) -> list[uuid.UUID]:
    """jsonb id 列表 → UUID（脏数据跳过，与历史回显的 stale-id 语义一致）。"""
    out: list[uuid.UUID] = []
    for v in raw or []:
        if not isinstance(v, str):
            continue
        try:
            out.append(uuid.UUID(v))
        except (ValueError, TypeError):
            continue
    return out


def _drain_start_source(
    item_out: dict, inner: Callable[[], AsyncIterator[SSEEvent]]
) -> Callable[[], AsyncIterator[SSEEvent]]:
    """排水回合事件源：首帧 turn_start（携带条目展示元数据），后续透传 inner。

    回合 SSE 流不携带用户消息事件，用户气泡的物化不能依赖 GET /queue 的
    轮询边界（sending 行存活整轮 run，边界回查存在重复/缺失竞态）；首帧
    随流确定性到达（live 首帧或观察流重放），前端按条目 id 幂等物化。
    inner 的确定性关闭与 run_source 同手法（防 asyncgen 终结器跨上下文）。
    """

    async def _source() -> AsyncIterator[SSEEvent]:
        yield SSEEvent(type="turn_start", payload=item_out)
        agen = inner()
        try:
            async for ev in agen:
                yield ev
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await agen.aclose()

    return _source


class QueueService:
    """进程内排队服务（lifespan 装配到 AppState.queue_service）。"""

    def __init__(self, st):
        self.st = st
        self._closed = False
        # per-session 锁：值对象仅在使用协程的局部引用存续期间存活（WeakValue
        # 语义下等待者持引用即保活，互斥不破），会话数增长不泄漏
        self._locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._drain_tasks: set[asyncio.Task] = set()

    async def shutdown(self) -> None:
        """停止续排并取消在飞排水任务（lifespan 关闭序：先于 RunManager）。"""
        self._closed = True
        tasks = list(self._drain_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------- 并发原语

    def lock(self, session_id: uuid.UUID) -> asyncio.Lock:
        """per-session 互斥锁（排水 claim→start 段 / withdraw / cancel 共用）。"""
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def spawn_drain(self, session_id: uuid.UUID) -> None:
        """fire-and-forget 排水：on_final 链安全（异常不外溢），task 强引用防 GC。"""
        task = asyncio.create_task(
            self._safe_drain(session_id), name=f"queue-drain-{str(session_id)[:8]}"
        )
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)

    async def _safe_drain(self, session_id: uuid.UUID) -> None:
        try:
            await self.maybe_drain(session_id)
        except Exception:
            logger.exception("queue drain failed for session %s (left in queue)", session_id)

    # ------------------------------------------------------------- 入队

    async def enqueue(
        self,
        session: dict,
        user_id: uuid.UUID,
        agent_id: uuid.UUID,
        content: str,
        *,
        rubric: Optional[str] = None,
        attachments: Optional[list] = None,
        knowledge_base_ids: Optional[list] = None,
    ) -> dict:
        """入队（上限 = pending+sending 计数，超出 QueueLimitError）。"""
        active = await dbmod.count_queued_by_status(
            self.st.pool, session["id"], ("pending", "sending")
        )
        if active >= self.st.settings.queue_max_per_session:
            raise QueueLimitError(
                f"待发队列已满（上限 {self.st.settings.queue_max_per_session} 条），"
                "请先清理或等待当前回合结束"
            )
        return await dbmod.insert_queued_message(
            self.st.pool,
            session["id"],
            user_id,
            agent_id,
            content,
            rubric,
            [a.upload_id for a in attachments or []],
            [str(k) for k in knowledge_base_ids or []],
        )

    async def wake_after_enqueue(self, session_id: uuid.UUID) -> None:
        """POST ③ 路径：清除 restart 挂起（新消息即恢复意图）并触发排水。

        error / withdrawn 挂起不清除——上轮失败/纠偏需用户显式 [继续]。
        """
        await dbmod.clear_queue_pause_if(self.st.pool, session_id, "restart")
        self.spawn_drain(session_id)

    # ------------------------------------------------------------- 排水

    async def maybe_drain(self, session_id: uuid.UUID) -> None:
        """排水入口（幂等）：paused 不排；认领队头 → 复检 → 启动。"""
        async with self.lock(session_id):
            await self._drain_locked(session_id)

    async def _drain_locked(self, session_id: uuid.UUID) -> None:
        if self._closed:
            return
        while True:
            if await dbmod.get_queue_pause(self.st.pool, session_id) is not None:
                return
            row = await dbmod.claim_next_queued(self.st.pool, session_id)
            if row is None:
                return
            try:
                outcome = await self._send_claimed(row)
            except Exception as e:  # noqa: BLE001 — 排水异常不滞留 sending
                logger.exception("queued item %s drain crashed", row["id"])
                await dbmod.mark_queued_failed(self.st.pool, row["id"], f"排水异常: {e}")
                outcome = _FAILED
            if outcome == _FAILED:
                continue  # 单条意图失效，继续消化下一条
            return  # started（递推交新 run）/ stop（回置 pending 留待下次）

    async def _send_claimed(self, row: dict) -> str:
        pool = self.st.pool
        session_id = row["session_id"]
        try:
            session, agent, config, attachments, kb_ids = await self._recheck(row)
        except _RecheckFailure as e:
            await dbmod.mark_queued_failed(pool, row["id"], str(e))
            logger.info("queued item %s failed recheck: %s", row["id"], e)
            return _FAILED

        from app.services.run_source import chat_event_source

        inner = chat_event_source(
            self.st,
            session,
            agent,
            config,
            agent_id=row["agent_id"],
            user_query=row["content"],
            attachments=attachments,
            knowledge_base_ids=kb_ids,
            rubric=row["rubric"],
            raw_content=row["content"],
            queue_item_id=str(row["id"]),
        )
        item_out = jsonable_encoder(await self.to_item_out(row))
        # 与手工直发同管道启动（usage 镜像 + touch + 409/503 映射）；
        # queue_hook=False：行落态与续排由 _drained_on_final 承担，不双触发
        from app.api.stream import _start_run_or_error

        try:
            await _start_run_or_error(
                self.st,
                session,
                kind="chat",
                factory=_drain_start_source(item_out, inner),
                on_final=self._drained_on_final(session_id, row["id"]),
                queue_hook=False,
            )
        except HTTPException:
            # RunConflict(409) / RunLimit(503)：行回置 pending，本轮排水中止
            await dbmod.reset_queued_to_pending(pool, row["id"])
            logger.info("queued item %s deferred (run conflict/limit)", row["id"])
            return _STOP
        return _STARTED

    async def _recheck(self, row: dict) -> tuple:
        """发送前复检：与手工直发完全相同的门禁序列，一个不少。

        任一失败 → _RecheckFailure（行置 failed 跳过续排，不暂停队列）。
        """
        pool = self.st.pool
        session = await dbmod.get_session(pool, row["session_id"])
        if session is None or session["status"] == "archived":
            raise _RecheckFailure("会话已归档或删除")
        if session.get("kind") == "cron":
            raise _RecheckFailure("任务会话不接受排队消息")
        agent_row = await dbmod.get_agent(pool, row["agent_id"])
        if agent_row is None:
            raise _RecheckFailure("入队时所选智能体已被删除")
        if agent_row["status"] != "active":
            raise _RecheckFailure(f"智能体「{agent_row['name']}」已归档")
        if agent_row["kind"] != "chat":
            raise _RecheckFailure(f"智能体「{agent_row['name']}」为定时任务专用")
        user = await dbmod.get_user(pool, row["user_id"])
        if user is None:
            raise _RecheckFailure("入队用户不存在")
        try:
            agent = await self.st.agent_registry.get_or_build(row["agent_id"])
        except KeyError as e:
            raise _RecheckFailure(f"智能体模型不可用: {e}") from e

        config = {
            "configurable": {
                "thread_id": session["thread_id"],
                "langgraph_auth_user": SimpleUser(
                    str(session["user_id"]), user.get("username")
                ),
            }
        }
        from app.api.stream import _refuse_or_heal_pending

        try:
            await _refuse_or_heal_pending(agent, config)
        except HTTPException as e:
            raise _RecheckFailure(str(e.detail)) from e
        except Exception as e:  # noqa: BLE001 — 状态读取/自愈失败同归单条失效
            raise _RecheckFailure(f"会话状态检查失败: {e}") from e

        attachments = [
            Attachment(upload_id=u) for u in (row["upload_ids"] or []) if isinstance(u, str)
        ]
        kb_ids = _parse_ids(row["kb_ids"])
        try:
            await validate_message_content(
                self.st,
                session,
                agent_id=row["agent_id"],
                attachments=attachments,
                knowledge_base_ids=kb_ids,
            )
        except MessageContentError as e:
            raise _RecheckFailure(e.detail) from e
        return session, agent, config, attachments, kb_ids

    def _drained_on_final(self, session_id: uuid.UUID, item_id: uuid.UUID):
        """被排水 run 的终态钩子：行落态（sent）+ 队列联动。

        interrupt / error / canceled 同置 sent——消息已随 run 首超步进历史，
        回合已被消耗；done 续排（spawn，不阻塞收尾）；error 挂起余队。
        """

        async def on_final(final_event) -> None:
            await dbmod.mark_queued_sent(self.st.pool, item_id)
            with contextlib.suppress(Exception):
                await dbmod.prune_sent_queued(
                    self.st.pool, session_id, self.st.settings.queue_sent_keep
                )
            if final_event.type == "done":
                self.spawn_drain(session_id)
            elif final_event.type == "error":
                await self.pause_if_pending(session_id, "error")

        return on_final

    # ------------------------------------------------------------- 终态联动

    async def on_run_final(self, session_id: uuid.UUID, final_type: str) -> None:
        """用户侧 run（send/resume/voice）终态钩子：done 续排、error 挂起。

        cron 会话队列恒空，天然 no-op。canceled / interrupt 不触发自动排水。
        """
        if final_type == "done":
            self.spawn_drain(session_id)
        elif final_type == "error":
            await self.pause_if_pending(session_id, "error")

    async def pause_if_pending(self, session_id: uuid.UUID, reason: str) -> None:
        """队列非空（有 pending/sending）才写暂停，避免空队列留误导态。"""
        if await dbmod.count_queued_by_status(
            self.st.pool, session_id, ("pending", "sending")
        ):
            await dbmod.pause_queue(self.st.pool, session_id, reason)

    # ------------------------------------------------------------- 视图

    async def get_queue_view(self, session_id: uuid.UUID) -> dict:
        """GET /queue 真值：整体态 + 条目（纯读，不触发排水）。"""
        rows = await dbmod.list_queued_messages(self.st.pool, session_id)
        reason = await dbmod.get_queue_pause(self.st.pool, session_id)
        has_active = any(r["status"] in ("pending", "sending") for r in rows)
        state = "idle" if not has_active else ("paused" if reason else "active")
        items = [await self.to_item_out(r) for r in rows]
        return {
            "state": state,
            "paused_reason": reason if has_active else None,
            "items": items,
        }

    async def to_item_out(self, row: dict) -> dict:
        """队列行 → QueuedMessageOut 形状（附件/KB 解析为展示元数据，
        与 /history 回显同构，供前端物化用户气泡与回撤锚定）。"""
        from app.services.session_service import _upload_row_to_out

        attachments: list[dict] = []
        upload_ids = _parse_ids(row["upload_ids"])
        if upload_ids:
            found = await dbmod.get_uploads_by_ids(
                self.st.pool, upload_ids, row["session_id"]
            )
            attachments = [
                _upload_row_to_out(found[str(p)]).model_dump()
                for p in upload_ids
                if str(p) in found
            ]
        kbs: list[dict] = []
        kb_ids = _parse_ids(row["kb_ids"])
        if kb_ids:
            rows = await dbmod.get_kbs_by_ids(self.st.pool, kb_ids)
            by_id = {str(r["id"]): r for r in rows}
            kbs = [
                {"id": by_id[str(p)]["id"], "name": by_id[str(p)]["name"]}
                for p in kb_ids
                if str(p) in by_id
            ]
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "content": row["content"],
            "rubric": row["rubric"],
            "attachments": attachments,
            "kbs": kbs,
            "status": row["status"],
            "fail_reason": row["fail_reason"],
            "created_at": row["created_at"],
        }
