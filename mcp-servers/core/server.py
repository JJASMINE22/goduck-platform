"""FastMCP server 核心：lifespan 装配 + 路由注册。

lifespan 在启动时建立:
  - MinIO client（附件字节持久层）
  - 第三方 MCP 工具池（通过 MultiServerMCPClient 加载 mcp_servers.json）
  - 知识库检索：embeddings client + VectorStoreFactory（store 本身 JIT 按需构建）

状态经 yield 注入每个工具调用的 ctx.request_context.lifespan_context，
无需全局变量（对标 FastAPI 的 Depends / Request.state）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from langchain_mcp_adapters.client import MultiServerMCPClient

import routers.document as document_router
import routers.image as image_router
import routers.knowledge as knowledge_router
from config import SERVER_HOST, SERVER_PORT, load_third_party_mcp_config
from routers._common import build_minio_client, ensure_tmp_dir
from services.embedding import build_embeddings
from services.vector_store import VectorStoreFactory

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(server: FastMCP):
    """启动时装配 MinIO + 第三方工具池，退出时由库自行清理。"""
    logger.info("正在加载第三方 MCP 配置...")
    third_party_config = load_third_party_mcp_config()

    # MinIO + 临时目录
    minio = build_minio_client()
    tmp_dir = ensure_tmp_dir()

    # 知识库检索：embeddings + ES vector factory（静态配置，一次构建常驻；
    # store 本身按 index JIT 创建+缓存，见 VectorStoreFactory.get_for_index）
    embeddings = build_embeddings()
    vector_factory = VectorStoreFactory(embeddings)

    # 第三方 MCP 工具池（连接复用，一次加载常驻）
    tools: dict = {}
    if third_party_config:
        client = MultiServerMCPClient(third_party_config, tool_name_prefix=False)
        tools_list = await client.get_tools()
        tools = {t.name: t for t in tools_list}
        logger.info("第三方工具加载完成: %s", list(tools.keys()))
    else:
        logger.warning("mcp_servers.json 未配置任何第三方 MCP")

    # markitdown 文档转 Markdown 引擎（进程级单例：首次构造加载 magika ONNX 模型 ~0.8s，
    # 此后所有 parse_document 调用复用此实例，摊薄为 0）。
    # try/except 隔离失败：markitdown 初始化异常不影响 image/knowledge 工具，仅 document 不可用。
    markitdown = None
    try:
        from markitdown import MarkItDown

        markitdown = MarkItDown()
        logger.info("markitdown 引擎就绪（docx/pdf → Markdown）")
    except Exception:
        logger.exception("markitdown 初始化失败；parse_document 将返回错误，其余工具不受影响")

    yield {
        "minio": minio,
        "tools": tools,
        "tmp_dir": tmp_dir,
        "vector_factory": vector_factory,
        "markitdown": markitdown,
    }
    logger.info("lifespan 结束，资源清理完成")


def create_app() -> FastMCP:
    """创建 FastMCP 实例并注册所有路由模块的工具。"""
    mcp = FastMCP(name="goduck-mcp-servers", lifespan=lifespan)
    # mcp.resource("tools")
    # mcp.tool("minio")
    # 注册各路由模块（每个模块导出 register(mcp)）
    image_router.register(mcp)
    document_router.register(mcp)
    knowledge_router.register(mcp)

    logger.info("已注册路由: image, document, knowledge")
    return mcp


# 模块级单例（供 import 使用）
app = create_app()


def run() -> None:
    """启动 MCP server（http transport）。"""
    app.run(transport="http", host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    run()
