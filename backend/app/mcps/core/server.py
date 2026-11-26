"""backend 内挂 FastMCP 服务（plan: vision-mcp-gdctx-knowledge W3）。

组织形态镜像旧 mcp-servers 网关：本模块装配 FastMCP 实例并注册 routers 的
工具；工具运行期经 ``app.mcps.bind.mcps_state()`` 取 AppState 闭包引用。
本进程同时是 MCP 客户端（MCPLifecycle 自消费 builtin 行）——同进程回环，
重活不落地：kb_search/vision 均轻量调用，docling/ingest 本就在 backend。

挂载契约（main.py lifespan，单进程单启动声明）::

    mcp_app = build_mcp_http_app()
    app.mount("/mcp", mcp_app)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_app.lifespan(app))  # session manager
        ...

FastMCP 的 StreamableHTTPSessionManager 要求父应用执行其 lifespan（`http_app()`
产物上的 ``.lifespan`` 为 asynccontextmanager 函数，调用后进入），否则请求期
报 "Task group is not initialized"。
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from app.mcps.routers import knowledge as knowledge_router
from app.mcps.routers import vision as vision_router

logger = logging.getLogger(__name__)


def create_mcp() -> FastMCP:
    """装配 FastMCP 实例并注册全部路由模块的工具。"""
    mcp = FastMCP(
        name="goduck",
        instructions="平台内置工具：知识库检索 kb_search；图像理解 image_understand。",
    )
    knowledge_router.register(mcp)
    vision_router.register(mcp)
    logger.info("builtin FastMCP ready (routers: knowledge, vision)")
    return mcp


# 模块级单例：工具注册随 import 完成（无状态），AppState 经 bind.py 运行期注入
mcp = create_mcp()


def build_mcp_http_app():
    """挂载用 ASGI 子应用（父应用 mount("/mcp", ...) 后端点为 /mcp）。"""
    return mcp.http_app(path="/")
