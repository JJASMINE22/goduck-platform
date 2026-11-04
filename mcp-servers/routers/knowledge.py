"""知识库检索路由：kb_search（向量模型/维度感知分组检索）。

链路：kb_ids → PG 注册表解析各库 embedding 行（失败回落 env）→ 一次
get_mapping 探测真实维度 → 按向量模型身份（或 legacy 维度）分组 → 每组
独立嵌入（组端点/维度查询向量）+ 一次 kNN（组内索引逗号拼接）→ 轮转归并。
ES 直连检索，PG 只读注册表（单条查询，fail-open）。

- 分组必要性：一次 kNN 的查询向量必须与各索引 mapping 同维且出自同一
  嵌入端点——同 dims 不同模型的库混组会得到维度合法、语义错误的静默错配。
- 归并策略：跨嵌入空间的 COSINE 分数不可比 → 轮转（round-robin）取各组
  按位结果，禁止裸分数排序。
- 组失败隔离：任一组检索异常（含注册维度≠mapping 的漂移库，跳过并告警）
  → 记日志跳过，不整体失败。

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


def _search_group(factory, index: str, dims: int, emb: dict | None, query: str, k: int):
    """单向量模型组检索（同步阻塞，调用方丢线程池）：组内一次 kNN。"""
    store = factory.get_for_index(index, emb, dims)
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
        registry = state["embedding_registry"]

        if not query.strip():
            return {"result": "error", "message": "检索词为空"}
        if not kb_ids:
            return {"result": "error", "message": "未指定知识库（kb_ids 为空）"}

        # ① 注册行解析（kb → embedding；未绑定/悬空/PG 不可达不在返回值中
        #    → 该库回落 legacy env 路径）+ 维度探测（一次 get_mapping 拿真值）。
        #    - ES 传输失败（不可达）→ error（真故障）；
        #    - 探测成功但全无索引（上传未解析/空库）→ 正常未命中（引导如实作答）。
        rows = await registry.resolve(kb_ids)
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

        # ② 分组：同注册行（或 legacy 同维度）一组；漂移库（注册维度≠mapping
        #    真值）跳过并告警——语义空间已错位，检索结果不可信，引导重解析。
        groups: dict[str, dict] = {}
        for kid in kb_ids:
            index_name = f"kb_{kid}"
            md = dims_map.get(index_name)
            if md is None:
                continue
            emb = rows.get(kid)
            if emb is not None and emb.get("dims") is not None and emb["dims"] != md:
                logger.warning(
                    "kb=%s 注册维度 %s 与索引实际 %s 不一致，跳过该库（请重新解析）",
                    kid, emb["dims"], md,
                )
                continue
            gkey = f"e:{emb['id']}" if emb is not None else f"d:{md}"
            g = groups.setdefault(gkey, {"emb": emb, "dims": md, "kids": []})
            g["kids"].append(kid)
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
        group_list = list(groups.values())

        async def _run_group(g: dict):
            index = ",".join(f"kb_{kid}" for kid in g["kids"])
            try:
                return await asyncio.to_thread(
                    _search_group, factory, index, g["dims"], g["emb"], query, k_per_group
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "kb_search 组检索失败 index=%s dims=%s: %s", index, g["dims"], e
                )
                return []

        group_hits = await asyncio.gather(*(_run_group(g) for g in group_list))

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
            "kb_search ok groups=%d keys=%s query=%s hits=%d",
            len(group_list), list(groups.keys()), query[:80], len(results),
        )
        return {
            "result": "ok",
            "query": query,
            "total": len(results),
            "hits": results,
        }
