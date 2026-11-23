"""RunManager：回合执行与 SSE 连接解耦（detached run + reattachable observer）。

设计（plan: sse-run-detach-reattach）：
- 回合是服务端资源：``start()`` 在进程内创建 run task 驱动事件源（chat_event_source
  / stream_adapter），SSE 响应与语音 WS 隧道退化为只读订阅者——订阅者断开
  （SPA 导航 / 刷新 / 断网）不再终止回合；
- 单写者闸：每 thread 至多一个 active run（RunConflictError），保 checkpointer
  一致性，同时吸收原 StreamTracker 的 withdraw / 双发防竞态职责；
- 重挂：事件携带 ``{token}.{seq}`` 的 SSE id；GET /sessions/{id}/stream 以
  Last-Event-ID / ?after 增量回放缓冲后转 live。跨 run 的旧 id（token 不匹配）
  自动降级为全量回放；观察者落后超出缓冲窗口 → ResyncNeeded（前端回落
  /history 真值重建）；
- 慢消费者隔离：订阅者各有界队列，满即被踢（收 resync 后关闭），run task
  永不 await 订阅者——一个卡死的标签页拖不垮回合；
- 内存有界：每 run 环形缓冲上限 + 进程并发 run 上限 + 终态 TTL sweeper。
  崩溃 / 重启语义与解耦前等价：缓冲与 run 俱失，前端回落 /history 恢复到
  最近 superstep（"不落盘"约束的天然边界）。

进程模型：单 worker 个人平台（同原 StreamTracker 前提）；多副本需外置
pub/sub（Redis）承载缓冲与广播，不在本实现范围。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)

# run 生命周期终止事件（契约与前端 TERMINAL_EVENTS 对齐，多出 stt_* 等非 run 类型）
TERMINAL_EVENT_TYPES = frozenset({"done", "interrupt", "error", "canceled"})

RunEventSource = Callable[[], AsyncIterator[SSEEvent]]
UsageCallback = Callable[[dict], Any]
FileChangeCallback = Callable[[dict, str], Any]  # (payload, run_token)
FinalCallback = Callable[[SSEEvent], Any]


class RunConflictError(Exception):
    """该 thread 已有 active run（单写者闸拒绝并发回合）。"""


class RunLimitError(Exception):
    """进程并发 run 上限已达（资源保护拒绝启动）。"""


class NoSuchRun(Exception):
    """该 thread 当前无 run（也无 TTL 窗口内的终态 RunState）。"""


class ResyncNeeded(Exception):
    """观察者落后超出缓冲窗口，无法增量对齐（前端应整体重建）。"""


# 慢消费者被踢时投递的队列哨兵（区别于任何真实 (seq, event) 元组）
_RESYNC = object()


@dataclass
class _Subscriber:
    """一个观察者连接（SSE / WS）的有界事件队列。"""

    queue: asyncio.Queue


@dataclass
class RunState:
    """单个 run 的全部内存态（执行 task + 回放缓冲 + 订阅者 + 终态）。"""

    thread_id: str
    session_id: uuid.UUID | None
    kind: str  # chat | resume | voice | cron
    token: str  # 本 run 的 id 前缀（跨 run 的旧 Last-Event-ID 据此识别）
    task: asyncio.Task
    buffer: deque = field(default_factory=deque)  # [(seq, SSEEvent)]，环形有界
    buffer_start: int = 1  # buffer[0] 的 seq（淘汰最老时右移）
    next_seq: int = 1
    terminal: SSEEvent | None = None
    terminal_seq: int | None = None
    created_at: float = field(default_factory=time.time)
    terminal_at: float | None = None
    subscribers: dict[int, _Subscriber] = field(default_factory=dict)
    on_usage: UsageCallback | None = None
    on_file_change: FileChangeCallback | None = None
    on_final: FinalCallback | None = None

    @property
    def done(self) -> bool:
        return self.terminal is not None


class RunManager:
    """进程内 run 注册表（lifespan 装配，start()/shutdown() 成对调用）。"""

    def __init__(self, settings):
        self._settings = settings
        self._runs: dict[str, RunState] = {}
        self._sub_seq = 0
        self._sweeper: asyncio.Task | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name="run-manager-sweeper"
            )

    async def shutdown(self, grace_s: float = 5.0) -> None:
        """停止 sweeper 并取消全部在飞 run（宽限等待由 gather 天然满足：取消即返回）。"""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        tasks = [r.task for r in self._runs.values() if not r.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
        logger.info("RunManager stopped (%d active run(s) cancelled)", len(tasks))

    # ------------------------------------------------------------------ run

    async def start_run(
        self,
        thread_id: str,
        session_id: uuid.UUID | None,
        kind: str,
        factory: RunEventSource,
        *,
        on_usage: UsageCallback | None = None,
        on_file_change: FileChangeCallback | None = None,
        on_final: FinalCallback | None = None,
    ) -> RunState:
        """启动一个 run（单写者闸：active run 存在则 RunConflictError）。

        终态后的 RunState 在 TTL 窗口内可被新 run 原地替换（晚归补领让位于
        新回合）。factory 为惰性事件源（run task 内首次迭代才执行——附件组装
        等前置消耗全部落在 run 生命周期内，withdraw 闸自 start 即生效）。
        """
        existing = self._runs.get(thread_id)
        if existing is not None and not existing.done:
            raise RunConflictError(f"thread {thread_id} 已有进行中的回合")
        active = sum(1 for r in self._runs.values() if not r.done)
        if active >= self._settings.run_max_concurrent:
            raise RunLimitError(
                f"并发回合上限（{self._settings.run_max_concurrent}）已达，请稍后重试"
            )
        run = RunState(
            thread_id=thread_id,
            session_id=session_id,
            kind=kind,
            token=uuid.uuid4().hex[:8],
            task=None,  # type: ignore[assignment]
            on_usage=on_usage,
            on_file_change=on_file_change,
            on_final=on_final,
        )
        run.task = asyncio.create_task(
            self._drive(run, factory), name=f"run-{thread_id[:12]}-{run.token}"
        )
        self._runs[thread_id] = run
        return run

    def cancel(self, thread_id: str) -> RunState | None:
        """显式取消 active run（广播 canceled 终态）；无 active run 返回 None。

        返回被取消的 RunState：调用方若需收尾图状态（清孤儿 pending，见
        api/stream.cancel_run），必须先等 run.task 真正终止（canceled 广播、
        on_final、生成器链 aclose 全部完成）再清理，否则清理会与仍在写入的
        checkpoint 竞争。仅查询真值语义的调用方按 None / 非 None 判断即可。
        """
        run = self._runs.get(thread_id)
        if run is None or run.done:
            return None
        run.task.cancel()
        return run

    def is_active(self, thread_id: str) -> bool:
        """该 thread 是否有进行中的 run（withdraw / cron 占用闸的查询面）。"""
        run = self._runs.get(thread_id)
        return run is not None and not run.done

    def peek(self, thread_id: str) -> bool:
        """该 thread 是否存在 RunState（active 或 TTL 窗口内终态）——通用存在性
        查询。观察流（observe_stream）的 204 预检请改用 is_replayable：终态 run
        只对携带本 run 水位的晚归观察者开放回放。"""
        return thread_id in self._runs

    def is_replayable(self, thread_id: str, last_event_id: str | None = None) -> bool:
        """observe_stream 的 204 预检：本次订阅是否应当发生。

        - active run：恒可订阅（冷进入全量补帧 / 携带水位增量对齐，行为不变）；
        - 终态 run（TTL 窗口内）：仅当客户端携带**本 run** 的水位（token 匹配）
          时可回放——晚归观察者补领终态事件的原始设计意图；无水位（冷进入）
          或跨 run 旧水位一律不可回放：已完结回合的结果 /history 即真值，
          回放会把整回合内容与 usage 再推一遍（前端重复渲染、重复累计，并
          催化"回放 done → 失效重拉 → 重挂 → 再回放"的自激环）。
        """
        run = self._runs.get(thread_id)
        if run is None:
            return False
        if not run.done:
            return True
        if not last_event_id:
            return False
        token, _, seq_s = last_event_id.rpartition(".")
        return token == run.token and seq_s.isdigit()

    # ------------------------------------------------------------- 订阅

    async def subscribe(
        self, thread_id: str, last_event_id: str | None = None
    ) -> AsyncIterator[tuple[str | None, SSEEvent]]:
        """订阅 run 事件流：先回放缓冲，再转 live，直至终态。

        yield ``(event_id, event)``；event_id 为 ``"{token}.{seq}"``（resync 哨兵
        为 None）。抛 NoSuchRun / ResyncNeeded（由调用方包装为空流 / resync 事件）。

        last_event_id 对齐规则：
        - token 匹配且 seq 可增量衔接（seq+1 >= buffer_start）→ 从 seq+1 回放；
        - token 不匹配（上一 run 的旧 id）→ 全量回放本 run；
        - token 匹配但落后超出缓冲窗口 → ResyncNeeded（存在不可补的空洞）。
        """
        run = self._runs.get(thread_id)
        if run is None:
            raise NoSuchRun(thread_id)
        last_seq: int | None = None
        if last_event_id:
            token, _, seq_s = last_event_id.rpartition(".")
            if token == run.token and seq_s.isdigit():
                last_seq = int(seq_s)
        if last_seq is not None and last_seq + 1 < run.buffer_start:
            raise ResyncNeeded(thread_id)

        # 先注册后快照：注册之后到达的事件进队列（live 阶段消费），回放与队列
        # 的重叠由 last_sent 去重——两者不重不漏。
        sub = _Subscriber(
            queue=asyncio.Queue(maxsize=self._settings.run_subscriber_queue_max)
        )
        self._sub_seq += 1
        sub_id = self._sub_seq
        run.subscribers[sub_id] = sub
        try:
            last_sent = run.buffer_start - 1 if last_seq is None else last_seq
            for seq, ev in list(run.buffer):
                if seq <= last_sent:
                    continue
                yield f"{run.token}.{seq}", ev
                last_sent = seq
            if run.terminal_seq is not None and last_sent >= run.terminal_seq:
                return  # 终态已在回放中交付
            while True:
                item = await sub.queue.get()
                if item is _RESYNC:
                    yield None, SSEEvent(
                        type="resync", payload={"reason": "observer-lagged"}
                    )
                    return
                seq, ev = item
                if seq <= last_sent:
                    continue  # 注册先于快照造成的重复
                last_sent = seq
                yield f"{run.token}.{seq}", ev
                if ev.type in TERMINAL_EVENT_TYPES:
                    return
        finally:
            run.subscribers.pop(sub_id, None)

    # ------------------------------------------------------------------ 内部

    async def _drive(
        self, run: RunState, factory: RunEventSource
    ) -> SSEEvent | None:
        """run task 主体：驱动事件源 → 编号/缓冲/广播 → 维护终态与收尾回调。

        返回终态事件（task.result()）；被取消时传播 CancelledError（终态已广播
        为 canceled，等待方自行 suppress）。
        """
        terminal: SSEEvent | None = None
        try:
            agen = factory()
            try:
                async for ev in agen:
                    if terminal is not None:
                        continue  # 防御：终态后事件不再广播（契约：终态必为最后一帧）
                    self._emit(run, ev)
                    if run.on_usage is not None and ev.type == "usage":
                        with contextlib.suppress(Exception):
                            await run.on_usage(ev.payload)
                    # file_change 镜像落库（usage 同款即时钩子；_emit 已先行广播，
                    # 落库延迟不影响 SSE 下发时序）
                    if run.on_file_change is not None and ev.type == "file_change":
                        with contextlib.suppress(Exception):
                            await run.on_file_change(ev.payload, run.token)
                    if ev.type in TERMINAL_EVENT_TYPES:
                        terminal = ev
                        # 不 break：继续驱动到生成器自然耗尽（return 路径）。
                        # break + aclose 会把生成器留在终态 yield 挂起态，随后从
                        # 其它执行上下文（asyncgen 终结器 task）关闭时，contextlib
                        # CM 的 Token 复位跨 Context 报 ValueError（Py3.12 实测），
                        # 并在 GeneratorExit 处理中触发 "async generator ignored
                        # GeneratorExit"；自然耗尽则让事件源自身的 with/finally
                        # 全部在原上下文内正常进出。
            finally:
                # 确定性关闭事件源生成器链（取消/异常路径；自然耗尽后为 no-op）
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await agen.aclose()
        except asyncio.CancelledError:
            canceled = SSEEvent(type="canceled", payload={"message": "回合已被取消"})
            self._emit(run, canceled)
            terminal = canceled
            raise
        except Exception as e:
            logger.exception("run %s crashed", run.token)
            err = SSEEvent(type="error", payload={"message": str(e)})
            self._emit(run, err)
            terminal = err
        finally:
            if terminal is None:
                # 防御：事件源未按契约产出终态即结束（外部 aclose 等）
                err = SSEEvent(type="error", payload={"message": "run 未产出终态事件即结束"})
                self._emit(run, err)
                terminal = err
            if run.on_final is not None:
                with contextlib.suppress(Exception):
                    await run.on_final(terminal)
        return terminal

    def _emit(self, run: RunState, ev: SSEEvent) -> None:
        """同步广播（无 await）：编号、缓冲、fan-out 在一个事件循环步内完成，
        天然原子——订阅者视角全局有序，run 永不被订阅者反压。"""
        seq = run.next_seq
        run.next_seq += 1
        run.buffer.append((seq, ev))
        while len(run.buffer) > self._settings.run_buffer_max_events:
            run.buffer.popleft()
            run.buffer_start += 1
        if ev.type in TERMINAL_EVENT_TYPES:
            run.terminal = ev
            run.terminal_seq = seq
            run.terminal_at = time.time()
        for sub in list(run.subscribers.values()):
            try:
                sub.queue.put_nowait((seq, ev))
            except asyncio.QueueFull:
                # 慢消费者：清空其积压并只投递 resync 哨兵（清空与投递之间无
                # await，put 必然成功——maxsize >= 1 由配置 gt=0 保证）
                while True:
                    try:
                        sub.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                sub.queue.put_nowait(_RESYNC)

    async def _sweep_loop(self) -> None:
        interval = self._settings.run_sweep_interval_seconds
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                self._sweep_once()

    def _sweep_once(self) -> None:
        """回收终态超 TTL 的 RunState；顺带清理 task 结束却无终态的防御路径。"""
        now = time.time()
        ttl = self._settings.run_ttl_seconds
        dead = [
            tid
            for tid, r in self._runs.items()
            if (
                r.done
                and r.terminal_at is not None
                and now - r.terminal_at > ttl
            )
            or (r.task.done() and not r.done)
        ]
        for tid in dead:
            self._runs.pop(tid, None)
