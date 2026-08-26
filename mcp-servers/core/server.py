"""FastMCP server 核心：lifespan 装配 + 路由注册。

附件解析（image_understand / parse_document）已迁移至 backend 进程：
图像描述经下挂视觉模型预消费（VisionDescriber），文档经 markitdown
预解析，结果在发送时确定性组装进 HumanMessage.content（见 backend
app/services/content_composer.py）。本进程仅保留知识库检索路由。

状态经 yield 注入每个工具调用的 ctx.request_context.lifespan_context，
无需全局变量（对标 FastAPI 的 Depends / Request.state）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP

import routers.knowledge as knowledge_router
from config import SERVER_HOST, SERVER_PORT
from services.vector_store import VectorStoreFactory

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(server: FastMCP):
    """启动时装配知识库检索依赖，退出时由库自行清理。"""
    # 知识库检索：ES vector factory（静态配置一次构建常驻；内部按 mapping dims
    # 分组懒构建 store 与嵌入客户端，见 VectorStoreFactory.get_dims/get_for_index）
    vector_factory = VectorStoreFactory()
    yield {"vector_factory": vector_factory}
    logger.info("lifespan 结束，资源清理完成")


def create_app() -> FastMCP:
    """创建 FastMCP 实例并注册所有路由模块的工具。"""
    mcp = FastMCP(name="goduck-mcp-servers", lifespan=lifespan)
    knowledge_router.register(mcp)
    logger.info("已注册路由: knowledge")
    return mcp


# 模块级单例（供 import 使用）
app = create_app()


def run() -> None:
    """启动 MCP server（http transport）。"""
    app.run(transport="http", host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    run()
