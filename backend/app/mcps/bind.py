"""AppState 绑定槽：FastMCP routers ↔ 后端进程内服务（替代旧网关 lifespan DI）。

旧 mcp-servers 网关经 ``ctx.request_context.lifespan_context`` 取装配产物；
收敛进 backend 后工具直接消费进程内已装配的服务（ModelStore /
KnowledgeService / MinIO），绑定发生在 main lifespan 装配完成之后
（``mcps_bind(state)``）。绑定前调用 ``mcps_state()`` 属装配顺序错误，
显式抛错（fail-visible）而非静默空转。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.deps import AppState

_state: Optional["AppState"] = None


def mcps_bind(state: "AppState") -> None:
    """注入 AppState 引用（main lifespan 内、yield 之前调用一次）。"""
    global _state
    _state = state


def mcps_state() -> "AppState":
    if _state is None:
        raise RuntimeError("AppState 未绑定到内置 MCP（main lifespan 装配顺序错误）")
    return _state
