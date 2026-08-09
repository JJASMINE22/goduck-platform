"""知识库检索路由：kb_search。

JIT 检索链路：kb_ids → kb_<id> 逗号拼接 → VectorStoreFactory.get_for_index 懒构建
store（不预载所有 ES 对象，按 index 组合缓存）。全链路 ES 直连，无 backend HTTP 跳。

工具名固定为 kb_search；经 mcp_server 表注册命名空间后变 {server}__kb_search，
正好命中 backend stream.py 的 *<注册名>__kb_search 扫描（见 content_composer 锚点）。

返回格式与 backend KnowledgeService.search 对齐：
  [{content, score, filename, doc_id, page, chunk_index, kb_id}]
"""
from __future__ import annotations

import asyncio
import logging

from fastmcp import Context, FastMCP

from config import KB_TOP_K

logger = logging.getLogger(__name__)


def register(mcp: FastMCP) -> None:
    """把知识库检索工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def kb_search(
        query: str,
        kb_ids: list[str],
        k: int = KB_TOP_K,
        ctx: Context = None,
    ) -> dict:
        """检索用户选择的知识库，返回 top-k 最相关文本片段。

        回答用户问题前请先调用此工具检索相关知识库内容，优先依据检索结果作答；
        未命中时如实说明，不得编造。

        Args:
            query: 检索词（用户问题或其改写）
            kb_ids: 知识库 id 列表（必须使用会话锚点中列出的真实 kb_id，禁止编造）
            k: 返回的 top-k 片段数（默认 5）
        """
        state = ctx.request_context.lifespan_context
        factory = state["vector_factory"]

        if not query.strip():
            return {"result": "error", "message": "检索词为空"}
        if not kb_ids:
            return {"result": "error", "message": "未指定知识库（kb_ids 为空）"}

        # kb_id → 索引名拼接（JIT：首次构建并缓存，后续命中）
        index = ",".join(f"kb_{kid}" for kid in kb_ids)
        store = factory.get_for_index(index)

        # ElasticsearchStore 的相似度检索是同步阻塞调用（HTTP → ES），
        # 放线程池避免阻塞 FastMCP 事件循环。
        try:
            hits = await asyncio.to_thread(
                store.similarity_search_with_score, query, k=k
            )
        except Exception as e:
            logger.warning("kb_search 失败 index=%s query=%s: %s", index, query[:80], e)
            return {"result": "error", "message": f"检索失败: {e}"}

        if not hits:
            return {
                "result": "ok",
                "query": query,
                "total": 0,
                "hits": [],
                "message": "未检索到相关内容，请如实告知用户并依据自身能力作答",
            }

        results: list[dict] = []
        for doc, score in hits:
            md = doc.metadata or {}
            results.append(
                {
                    "content": doc.page_content,
                    "score": float(score),
                    "filename": md.get("filename", ""),
                    "doc_id": md.get("doc_id"),
                    "page": md.get("page"),
                    "chunk_index": md.get("chunk_index"),
                    "kb_id": md.get("kb_id"),
                }
            )

        logger.info(
            "kb_search ok index=%s query=%s hits=%d",
            index, query[:80], len(results),
        )
        return {
            "result": "ok",
            "query": query,
            "total": len(results),
            "hits": results,
        }
