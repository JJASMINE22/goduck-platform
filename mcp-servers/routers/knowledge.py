"""知识库检索路由：kb_search（维度感知分组检索）。

链路：kb_ids → 一次 get_mapping 探测各库真实维度 → 按 dims 分组 → 每组独立
嵌入（组维度查询向量）+ 一次 kNN（组内索引逗号拼接）→ 轮转归并。全链路 ES
直连，无 backend HTTP 跳、无 PG 依赖。

- 分组必要性：各库 dims 可不同（per-KB 维度）；一次 kNN 的查询向量维度必须与
  各索引 mapping 一致，跨维混合查询会被 ES 400 整体拒绝。
- 归并策略：跨嵌入空间的 COSINE 分数不可比 → 轮转（round-robin）取各组
  按位结果，禁止裸分数排序。
- 组失败隔离：任一组检索异常（含维度错位兜底）→ 记日志跳过该组，不整体失败。

工具名固定为 kb_search；经 mcp_server 表注册命名空间后变 {server}__kb_search，
正好命中 backend stream.py 的 *<注册名>__kb_search 扫描（见 content_composer 锚点）。

返回格式与 backend KnowledgeService.search 对齐：
  [{content, score, filename, doc_id, page, chunk_index, kb_id}]
"""
from __future__ import annotations

import asyncio
import logging
import math

from fastmcp import Context, FastMCP

from config import KB_TOP_K

logger = logging.getLogger(__name__)


def _search_group(factory, index: str, dims: int, query: str, k: int):
    """单维度组检索（同步阻塞，调用方丢线程池）：组内一次 kNN。"""
    store = factory.get_for_index(index, dims)
    return store.similarity_search_with_score(query, k=k)


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

        # ① 维度探测：一次 get_mapping 拿全部所选库的真实 dims。
        #    - 传输失败（ES 不可达）→ error（真故障）；
        #    - 探测成功但全无索引（上传未解析/空库）→ 正常未命中（引导如实作答，
        #      不作故障上报——未解析属用户未执行操作，非检索链路问题）。
        try:
            dims_map = await asyncio.to_thread(factory.get_dims, kb_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("kb_search 维度探测失败 kb_ids=%s: %s", kb_ids, e)
            return {"result": "error", "message": f"知识库存储不可达: {e}"}
        if not dims_map:
            return {
                "result": "ok",
                "query": query,
                "total": 0,
                "hits": [],
                "message": "所选知识库暂无可检索内容（文档可能尚未解析入库），"
                "请如实告知用户并依据自身能力作答",
            }

        # ② 按 dims 分组（组内维度一致才可共用一次 kNN）；空库跳过
        groups: dict[int, list[str]] = {}
        for kid in kb_ids:
            index_name = f"kb_{kid}"
            dims = dims_map.get(index_name)
            if dims is None:
                continue
            groups.setdefault(dims, []).append(index_name)
        if not groups:
            return {
                "result": "ok",
                "query": query,
                "total": 0,
                "hits": [],
                "message": "未检索到相关内容，请如实告知用户并依据自身能力作答",
            }

        # ③ 每组独立检索（并发丢线程池；单组失败跳过不整体失败）
        k_per_group = max(1, math.ceil(k / len(groups)))
        group_list = list(groups.items())

        async def _run_group(dims: int, indexes: list[str]):
            index = ",".join(indexes)
            try:
                return await asyncio.to_thread(_search_group, factory, index, dims, query, k_per_group)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "kb_search 组检索失败 index=%s dims=%s: %s", index, dims, e
                )
                return []

        group_hits = await asyncio.gather(*(
            _run_group(dims, indexes) for dims, indexes in group_list
        ))

        # ④ 轮转归并（跨组分数不可比）：各组按分数降序逐位交错，截断到 k
        merged: list[tuple] = []
        for i in range(k_per_group):
            for hits in group_hits:
                if i < len(hits):
                    merged.append(hits[i])
        merged = merged[:k]

        if not merged:
            return {
                "result": "ok",
                "query": query,
                "total": 0,
                "hits": [],
                "message": "未检索到相关内容，请如实告知用户并依据自身能力作答",
            }

        results: list[dict] = []
        for doc, score in merged:
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
            "kb_search ok groups=%d dims=%s query=%s hits=%d",
            len(group_list), [d for d, _ in group_list], query[:80], len(results),
        )
        return {
            "result": "ok",
            "query": query,
            "total": len(results),
            "hits": results,
        }
