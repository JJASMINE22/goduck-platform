"""Cron scheduler: DB-driven tick loop + claim-then-execute runner.

设计要点（与平台"数据驱动运行时"同构）：
- DB 是唯一真相源：每 tick 轮询 due 行（enabled AND next_run_at <= now()），
  无内存任务注册表——创建/更新落库即生效，重启天然恢复。
- 认领优先于执行（claim-then-execute）：UPDATE ... WHERE next_run_at = 观察值
  原子推进下一槽位后再派发。崩溃时序上"丢一轮"（run 历史可见空档）优于
  "重复执行一轮"（重复烧钱）。多副本部署下该乐观并发天然防双触发。
- misfire 策略：next_slot 沿 start_at 锚定相位跳到未来槽位——停机/跳过期间
  错过 N 个槽位不补发，最多在恢复后跑一次。
- tick 与执行解耦：tick 只做判断+认领+派发（毫秒级返回），执行在独立 task
  里过信号量（全局并发上限）+ per-job 运行锁（防重叠）。
- 无人值守禁 HITL：cron 智能体配置层已禁 interrupt_on（validator 拒绝 +
  构建期剥离双保险），运行期不应出现中断；万一旁路触发按 error run 计
  （fail-visible）。tick 门②/run_now 对 awaiting_approval 的挂起检查保留为
  防御性遗留（兼容直写 DB 等异常路径，经 /sessions/{id}/resume 补批后恢复）。
- 每轮处女线程（fresh context）：注入前重置专用线程（adelete_thread 硬删
  messages/摘要侧通道/files/checkpoint_writes），模型只见系统提示+平台记忆+
  本轮 prompt——轮次相互独立，累积历史只会线性抬高输入成本、并让旧轮产出
  （含其摘要）污染本轮（见 _reset_thread_context）。

进程模型：与 StreamTracker 一致的单进程假设（个人平台单 worker）。多副本需
改为 SELECT ... FOR UPDATE SKIP LOCKED 认领。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from langgraph.graph.state import CompiledStateGraph

from app.core.schema_utils import cron_unattended_violations, tree_allows_shell
from app.core.user_scoped_backend import user_workspace_path
from app.deps import AppState, SimpleUser
from app.models import db as dbmod

try:  # deepagents 未 pin 版本：常量缺失时回退字面量（键名本身是稳定契约）
    from deepagents.middleware.summarization import SUMMARIZATION_SESSION_ID_KEY
except ImportError:  # pragma: no cover - depends on installed versions
    SUMMARIZATION_SESSION_ID_KEY = "_summarization_session_id"

logger = logging.getLogger(__name__)

# 流终止事件（done/error）→ 任务运行状态。interrupt 不在此映射：cron 已禁
# HITL（validator + 构建期剥离双保险），万一仍出现中断按 error 兜底
# （fail-visible，见 _invoke），绝不进入 awaiting_approval 挂起。
_STATUS_BY_EVENT = {
    "done": "ok",
    "error": "error",
}


def next_slot(prev: datetime, interval_minutes: int, now: datetime) -> datetime:
    """沿相位格点推进到严格未来：nxt = prev + k*interval > now（k 取最小正整数）。

    prev 是上次槽位（due 的 next_run_at），相位由此保持（锚定 start_at）；
    停机/跳过积压的槽位直接迈过（misfire 不补发）。纯函数，便于单测。
    """
    step = timedelta(minutes=interval_minutes)
    nxt = prev + step
    while nxt <= now:
        nxt += step
    return nxt


class CronScheduler:
    """Lifespan 常驻调度循环。start()/shutdown() 由 main.py 装配。"""

    def __init__(self, st: AppState):
        self.st = st
        self.settings = st.settings
        self._sem = asyncio.Semaphore(self.settings.cron_max_concurrency)
        self._running: set[str] = set()      # per-job 运行锁（进程内，job id 字符串）
        self._tasks: set[asyncio.Task] = set()  # 在飞执行 task（优雅停机收割用）
        self._stop = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="cron-scheduler")

    async def shutdown(self, grace_s: float = 30.0) -> None:
        """停止循环 + 收割在飞运行（宽限内等待，超时取消；checkpointer 保证
        中途取消的 thread 状态一致，最坏丢一轮）。"""
        self._stop.set()
        if self._loop_task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._loop_task), timeout=5)
            if not self._loop_task.done():
                self._loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._loop_task
        if self._tasks:
            logger.info("cron scheduler: waiting for %d in-flight run(s)", len(self._tasks))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=grace_s
                )
            for t in self._tasks:
                t.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("cron scheduler stopped")

    # ------------------------------------------------------------------ loop

    async def _loop(self) -> None:
        # 启动即先 tick：停机期间到期的任务按 misfire 策略（跳到未来槽位）处理。
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单次 tick 失败不杀死循环
                logger.exception("cron tick failed (continuing)")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.cron_tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        for job in await dbmod.due_cron_jobs(self.st.pool):
            jid = str(job["id"])
            # ① 运行锁：上次还没跑完（如 interval 1h、单次跑了 2h）→ 本槽跳过
            if jid in self._running:
                await self._skip(job, now, "overlap: 上次运行尚未结束")
                continue
            # ② HITL 挂起（防御性遗留门：interrupt 已映射 error，正常路径不再
            # 产生该状态；保留兜底直写 DB / 旁路异常，避免无人消费的挂起僵尸）
            if job["last_status"] == "awaiting_approval":
                await self._skip(job, now, "awaiting_approval: 等待用户审批挂起中")
                continue
            # ③ 用户侧流占用（审批 resume 进行中）→ 延后到下个槽位
            thread_id = job.get("thread_id")
            if thread_id and self.st.stream_tracker.is_streaming(thread_id):
                await self._skip(job, now, "busy: 会话正被用户侧流占用")
                continue
            # ④ 原子认领（乐观并发推进 next_run_at），竞态输掉则静默让出
            claimed = await dbmod.claim_cron_job(
                self.st.pool, job["id"], job["next_run_at"],
                next_slot(job["next_run_at"], job["interval_minutes"], now),
            )
            if claimed is None:
                continue
            # ⑤ 派发：不 await —— tick 必须秒回，否则一个慢 agent 阻塞所有任务触发
            self._dispatch(job, trigger="scheduled")

    async def _skip(self, job: dict, now: datetime, reason: str) -> None:
        """跳过槽位：仅推进相位（保 start_at 锚定格点），不写 run 行——run
        历史只记真实执行，跳过原因进日志（频率=每 tick 一次，可接受）。"""
        await dbmod.skip_cron_job(
            self.st.pool, job["id"], job["next_run_at"],
            next_slot(job["next_run_at"], job["interval_minutes"], now),
        )
        logger.info("cron job '%s' skipped slot (%s)", job.get("name"), reason)

    # --------------------------------------------------------------- manual

    async def run_now(self, job: dict) -> tuple[bool, str]:
        """手动立即执行一次（测试/补跑）。不推进 next_run_at，不写认领。

        返回 (ok, reason)：ok=False 时 reason 为 409 细节。禁用态任务允许手动
        触发（这正是暂停任务补跑/验证的正确姿势）。
        """
        jid = str(job["id"])
        if jid in self._running:
            return False, "任务正在运行中"
        if job.get("last_status") == "awaiting_approval":
            return False, "任务挂起等待审批，请先在任务会话中完成 HITL 审批"
        thread_id = job.get("thread_id")
        if thread_id and self.st.stream_tracker.is_streaming(thread_id):
            return False, "任务会话正被占用（审批流进行中），请稍后重试"
        # 无人值守禁用能力（纵深第三层，fail-closed：手动路径直接 409）：
        # shell / 工具审批 / 附件 / 知识库
        violations = await self._agent_unattended_violations(job["agent_id"])
        if violations:
            return False, (
                f"绑定智能体配置树含无人值守禁用能力（{'、'.join(violations)}），"
                "任务不可执行（请关闭对应能力或换绑）"
            )
        self._dispatch(job, trigger="manual")
        return True, "started"

    def _dispatch(self, job: dict, trigger: str) -> None:
        jid = str(job["id"])
        self._running.add(jid)
        task = asyncio.create_task(self._execute(job, trigger=trigger), name=f"cron-run-{jid[:8]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --------------------------------------------------------------- execute

    async def _execute(self, job: dict, trigger: str = "scheduled") -> None:
        jid = str(job["id"])
        run = await dbmod.insert_cron_run(self.st.pool, job["id"], trigger=trigger)
        if trigger != "scheduled":
            # 手动触发无认领（claim）步骤：运行中状态/上次运行时间在此补写——
            # 任务列表与会话页的「运行中」徽标由此驱动（调度触发由 claim 原子完成）。
            with contextlib.suppress(Exception):
                await dbmod.update_cron_job(
                    self.st.pool,
                    job["id"],
                    {"last_status": "running", "last_run_at": datetime.now(timezone.utc)},
                )
        status, err = "error", None
        try:
            # 触发前防御复查（plan §七.2，纵深第三层）：无人值守禁用能力
            # （shell/审批/附件/知识库）。不静默跳过——计为 error run
            # （fail-visible），连续失败照常熔断。
            violations = await self._agent_unattended_violations(job["agent_id"])
            if violations:
                status, err = (
                    "error",
                    (
                        f"绑定智能体配置树含无人值守禁用能力（{'、'.join(violations)}），"
                        "任务不可执行（请关闭对应能力或换绑后重试）"
                    ),
                )
            else:
                async with self._sem:
                    status, err = await asyncio.wait_for(
                        self._invoke(job), timeout=self.settings.cron_run_timeout_s
                    )
        except asyncio.TimeoutError:
            status, err = "error", f"运行超时（>{int(self.settings.cron_run_timeout_s)}s）已中止"
        except Exception as e:  # noqa: BLE001 — 单任务失败熔断不外溢
            status, err = "error", f"{type(e).__name__}: {e}"
            logger.exception("cron job '%s' execution crashed", job.get("name"))
        finally:
            self._running.discard(jid)
            with contextlib.suppress(Exception):
                await dbmod.finish_cron_run(self.st.pool, run["id"], status, err)
            with contextlib.suppress(Exception):
                await dbmod.finish_cron_job_run(
                    self.st.pool, job["id"], status, err,
                    self.settings.cron_max_consecutive_failures,
                )
            if status == "error":
                logger.error("cron job '%s' run failed: %s", job.get("name"), err)
            else:
                logger.info("cron job '%s' run finished (%s)", job.get("name"), status)

    async def _agent_unattended_violations(self, agent_id: uuid.UUID) -> list[str]:
        """防御性复查 agent 当前配置树的无人值守禁用能力（以能力为准，不以
        声明为准）：shell + 审批/附件/知识库。agent 行缺失按"无违规"处理——
        后续 get_or_build 自会以 KeyError 计为 error run，此处不重复拦截。"""
        row = await dbmod.get_agent(self.st.pool, agent_id)
        if row is None:
            return []
        config = row.get("config_json") or {}
        violations = cron_unattended_violations(config)
        if tree_allows_shell(config):
            violations.append("allow_shell")
        return violations

    async def _invoke(self, job: dict) -> tuple[str, Optional[str]]:
        """一次任务执行：取图 → 会话保障 → 线程重置 → 与普通会话完全同构的注入。

        消费 StreamAdapter 事件流（无 SSE sink）：usage 即时入库、终态事件
        （done/interrupt/error 契约）映射为任务状态（interrupt 计为 error：
        cron 禁 HITL，中断无人消费）。文件变更徽标/摘要等
        中间件行为与用户会话零差异（_run 内部 activate 的 collector 照常工作）。

        上下文策略（与聊天会话的分野）：每轮注入前重置线程
        （_reset_thread_context），模型只见系统提示 + 平台记忆(/memories) +
        本轮 prompt——定时任务是相互独立的单次执行，累积历史只会线性抬高
        输入成本、并让旧轮产出（含其摘要）污染本轮。
        """
        agent = await self.st.agent_registry.get_or_build(job["agent_id"])
        session = await self._ensure_session(job)
        thread_id = session["thread_id"]
        config = {
            "configurable": {
                "thread_id": thread_id,
                "langgraph_auth_user": SimpleUser(job["user_id"]),
            }
        }

        # 双检 stream_tracker：tick 检查后到此处之间的用户侧 resume 竞态兜底。
        # 已认领（next_run_at 已推进），该轮计为 error 而非静默丢弃（run 可见）。
        if self.st.stream_tracker.is_streaming(thread_id):
            return "error", "会话正被用户侧流占用，本轮放弃（下个槽位自动恢复）"

        final = None
        self.st.stream_tracker.acquire(thread_id)
        try:
            # 持锁后、注入前重置：与用户侧 resume（同锁串行）互斥，重置绝不落在
            # 活跃流上；awaiting_approval 门卫（tick 门②/run_now）保证不会误删
            # 待审批现场。残余竞态（resume 已过 interrupt 校验、尚未 acquire 的
            # 亚毫秒窗口）最坏表现为该次 resume 报错一次，接受。
            await self._reset_thread_context(agent, config, thread_id, job["user_id"])
            async for ev in self.st.stream_adapter.stream_message(
                agent, job["prompt"], config, raw_content=job["prompt"]
            ):
                if ev.type == "usage":
                    # token 用量即时入库（best-effort，失败不打断运行；同 stream.py）
                    with contextlib.suppress(Exception):
                        await dbmod.add_session_usage(self.st.pool, session["id"], ev.payload)
                final = ev
        finally:
            self.st.stream_tracker.release(thread_id)
            with contextlib.suppress(Exception):
                await dbmod.touch_session(self.st.pool, session["id"])

        # 契约：流必以 done | interrupt | error 收尾；防御性兜底到 error。
        ftype = final.type if final is not None else "error"
        if ftype == "interrupt":
            # cron 已禁 HITL（validator 拒绝 + 构建期剥离双保险），此处为旁路
            # 兜底：中断在无人值守下无消费方，计为 error run（fail-visible），
            # 绝不进入 awaiting_approval 挂起（会无限跳槽成僵尸任务）。
            return (
                "error",
                (
                    "运行触发了工具审批中断（cron 智能体已禁用 HITL），"
                    "请检查智能体配置的 interrupt_on"
                ),
            )
        status = _STATUS_BY_EVENT.get(ftype, "error")
        err = final.payload.get("message") if (final is not None and ftype == "error") else None
        return status, err

    async def _reset_thread_context(
        self,
        agent: CompiledStateGraph,
        config: dict,
        thread_id: str,
        user_id: str,
    ) -> None:
        """重置任务专用线程为处女状态（fresh context per run）。

        与 withdraw 的软删（RemoveMessage，原 writes 留影子）不同，这里直接
        adelete_thread 硬删该线程全部 checkpoint：messages、摘要侧通道
        _summarization_event、files 虚拟文件（StateBackend，含
        /conversation_history/ 卸载）、checkpoint_writes——cron 语义不保留
        任何跨轮状态，硬删最简且无影子累积。线程随本轮 astream 自然重生，
        session/thread_id 身份不变 → /history 与 HITL 端点零改动。

        磁盘卸载残留：allow_shell 的 agent 默认后端是磁盘型，摘要逐出的历史
        落在 <用户workspace>/conversation_history/{sid}.md（sid 持久于线程
        状态、线程间互不共享）——先读状态取本线程 sid，删线程后精确 unlink
        本线程那份，不动同用户其他会话/任务的历史文件；media/ 子目录为全体
        共享的哈希命名、无法归属线程，留作存储增长项（非污染面，agent 不主动
        读取）。/workspace/ 产出与 /memories/ 均在线程状态之外，刻意存活。

        失败语义：重置是本轮运行的正确性前提（不重置=旧史污染照旧），异常
        向上抛出、由 _execute 计为 run error（熔断可见），不静默降级。
        """
        # 必须先读后删：sid 存活在即将被删的线程状态里
        state = await agent.aget_state(config)
        sid = (state.values or {}).get(SUMMARIZATION_SESSION_ID_KEY)
        await self.st.checkpointer.adelete_thread(thread_id)
        if isinstance(sid, str) and sid:
            offload = (
                user_workspace_path(self.settings.workspace_dir, user_id)
                / "conversation_history"
                / f"{sid}.md"
            )
            # best-effort：文件缺失/不可删只影响磁盘整洁，不影响本轮正确性
            with contextlib.suppress(OSError):
                offload.unlink(missing_ok=True)

    async def _ensure_session(self, job: dict) -> dict:
        """专用会话保障：agent 更新会级联归档会话（同时任务已被停用，这里是
        手动触发/竞态路径的兜底）。行丢失（理论路径）按原 thread_id 不可恢复，
        生成新 thread 重建——checkpointer 历史随之翻篇，run 可见可追溯。"""
        session = await dbmod.get_session(self.st.pool, job["session_id"])
        if session is None:
            session = await dbmod.create_session(
                self.st.pool,
                agent_id=job["agent_id"],
                user_id=job["user_id"],
                thread_id=f"thread-{uuid.uuid4().hex}",
                title=f"[定时] {job['name']}",
                kind="cron",
            )
            await dbmod.update_cron_job(self.st.pool, job["id"], {"session_id": session["id"]})
            logger.warning(
                "cron job '%s': dedicated session row missing; recreated (thread reset)",
                job["name"],
            )
        elif session["status"] == "archived":
            await self._assert_agent_usable(job["agent_id"])
            revived = await dbmod.reactivate_cron_session(self.st.pool, session["id"])
            if revived is not None:
                session = revived
        return session

    async def _assert_agent_usable(self, agent_id: uuid.UUID) -> None:
        row = await dbmod.get_agent(self.st.pool, agent_id)
        if row is None:
            raise KeyError(f"agent {agent_id} not found")
        if row["status"] == "archived":
            raise ValueError(f"智能体 '{row['name']}' 已归档，任务不可执行（请先恢复智能体）")
        if row.get("kind") != "cron":
            # 纵深防御：理论上启动规范化已停用此类任务，此处兜底 legacy/竞态路径
            raise ValueError("绑定的智能体非定时任务类型（kind!=cron）")
