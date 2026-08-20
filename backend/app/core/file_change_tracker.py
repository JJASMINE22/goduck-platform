"""工作区文件变更埋点：contextvar 收集器 + difflib 行级差异计数。

职责边界（纯观测，不改任何写入语义）：
- TrackedUserScopedFilesystemBackend 在落盘瞬间做"前读 → super → 后读 → difflib 计数"，
  把 FileChangeRecord append 进当前 SSE 请求任务作用域的收集器；
- StreamAdapter 在流式期间 ``activate()``，每个 updates 块后 ``drain()`` 并推送
  ``file_change`` SSE 事件。

并发隔离（task 级，非 user 级）：
- ``_collector`` 是 ContextVar——名字模块级唯一，值随 execution context 隔离。
  每个 SSE 请求是独立 asyncio task（创建时 copy_context 快照），task 内 set 只改
  自己的副本；多用户并发请求互不可见。
- LangGraph 并行子代理的子 task 在 astream 任务树内创建，继承 activate 后的
  context，多子代理写入汇入同一收集器（一个会话一个时间线，符合期望）。
- 工具执行走 ``asyncio.to_thread``（= copy_context + 线程内 ctx.run），contextvar
  随调用传播，工具线程的记录落回 SSE 任务的收集器。
- 若未来某路径改用 ``loop.run_in_executor``（不传播 contextvar），collector 为
  None → record_change 静默直通：最坏丢一条徽标，绝不会记到别人头上（失效方向安全）。
- 未 activate（单测直接调 backend / 非流式调用）时 record_change / drain 均为
  零开销直通，行为与不埋点完全一致。

Lock 的必要性（微观竞态）：drain 在 SSE 任务上"取走旧表 + 换入空表"，append 在
工具线程（to_thread）——无锁时 append 可能落在刚被换走的旧表上：该记录既不在
本轮 drain 返回值里、也不再出现在收集器（永久丢失）。锁把 swap 与 append 互斥；
临界区仅一两个 list 操作，纳秒级，无争用之虞。
"""
from __future__ import annotations

import difflib
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Literal

Action = Literal["create", "edit", "delete"]

# 超限不做行级计数（added/removed=None，仅记动作）：SequenceMatcher 最坏 O(n²)，
# 512KB 源码 ≈ 万行级、毫秒级完成，超过则放弃精确计数只保留事件本身。
MAX_DIFF_BYTES = 512 * 1024


@dataclass
class FileChangeRecord:
    """一次落盘动作的行级差异摘要。

    path 相对用户工作区根（CompositeBackend 已剥掉 /workspace/ 前缀，与浏览 API
    的 WorkspaceNode.path 同源，前端可直接匹配）。added/removed 为 None 表示
    二进制 / 超大 / 目录——前端只显动作徽标，不显 +N −M。
    """

    path: str
    action: Action
    added: int | None = None
    removed: int | None = None
    # 仅调试用途（record 排序/延迟观测），不进 SSE payload。
    ts: float = field(default_factory=time, repr=False)

    def to_payload(self) -> dict:
        """SSE payload 形状（stream.ts FileChangeRecord 一一对应）。"""
        return {"path": self.path, "action": self.action, "added": self.added, "removed": self.removed}


class _Collector:
    """单请求任务作用域的记录缓冲（见模块 docstring 的锁论证）。"""

    __slots__ = ("lock", "records")

    def __init__(self) -> None:
        self.records: list[FileChangeRecord] = []
        self.lock = threading.Lock()


_collector: ContextVar[_Collector | None] = ContextVar("file_change_collector", default=None)


@contextmanager
def activate():
    """激活收集器（StreamAdapter._run 进入时调用）。

    yield 出 collector 实例本身：SSE 任务可在 with 退出后（error 路径，contextvar
    已被 finally 复位）仍通过 drain(collector) 取走残留记录。

    时序约束：activate 必须包住 ``agent.astream(...)`` 的整个迭代——并行子代理的
    子 task 在迭代中创建，创建时快照的 context 必须已含 collector；不得移入循环内。
    客户端 abort → async generator aclose → GeneratorExit → finally → reset，
    contextvar 复位，无跨请求泄漏。
    """
    c = _Collector()
    tok = _collector.set(c)
    try:
        yield c
    finally:
        _collector.reset(tok)


def record_change(r: FileChangeRecord) -> None:
    """记录一次落盘变更；未激活（单测/非流式）时静默直通，零行为差异。"""
    c = _collector.get()
    if c is not None:
        with c.lock:
            c.records.append(r)


def drain(collector: _Collector | None = None) -> list[FileChangeRecord]:
    """取走并清空当前（或指定）收集器的记录。

    指定 collector 用于 error 路径：with 块退出后 contextvar 已复位，但 StreamAdapter
    仍持有 activate() yield 出的实例引用，可取走最后一批记录再发 error 事件。
    """
    c = collector if collector is not None else _collector.get()
    if c is None:
        return []
    with c.lock:
        out, c.records = c.records, []
    return out


def count_line_diff(old: str, new: str) -> tuple[int, int]:
    """行级差异计数：返回 (added, removed)。

    语义与编辑器 / ``git diff --stat`` 一致：
    - replace 区段：旧行计 removed，新行计 added（一次改写两边都算）；
    - delete 区段：仅 removed；insert 区段：仅 added；equal 跳过。

    为什么用 ``SequenceMatcher.get_opcodes()`` 而非 ``unified_diff``：后者产出
    diff 文本需再解析 +/- 前缀行统计（结构化结果当文本二次解析，脆弱）；
    opcodes 直接给出语义区段，按区段类型累加即可。

    细节：
    - ``autojunk=False`` 必须显式传：默认 autojunk 会把出现频率 >1% 的"热门行"
      （代码里的 ``}``、空行、``---`` 分隔线）当 junk 剪枝，对典型代码文件计数
      严重失真；
    - ``splitlines(keepends=False)``：统一按逻辑行比较，文件末尾 ``\\n`` 有无不
      计为差异（与 VS Code 行为一致）。
    """
    a = old.splitlines()
    b = new.splitlines()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def read_text_if_diffable(full_path: Path) -> str | None:
    """<=MAX_DIFF_BYTES 且 UTF-8 可解码且无 NUL（二进制信号）→ 文本；否则 None。

    纯观测读：任何读取失败（不存在 / 权限 / 编码）一律返回 None，绝不让埋点
    影响写入路径。
    """
    try:
        if not full_path.is_file():
            return None
        if full_path.stat().st_size > MAX_DIFF_BYTES:
            return None
        data = full_path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
