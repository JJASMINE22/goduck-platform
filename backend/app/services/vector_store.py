"""Elasticsearch 向量库工厂（知识库 RAG）。

一库一索引：index_name = `kb_<kb_id>`。
- get(kb_id, dims)：按 (kb_id, dims) 缓存单库 store（写路径：入库/删文档）。
  dims 为该库记录的嵌入维度（kb.dims；None=全局默认/模型原生维度）。
- get_for_index(index, dims)：通配/多索引 store（index='kb_a,kb_b'，仅限同维度组，
  调用方分组后进入）；读路径跨库检索。
- get_dims(kb_id)：读 ES mapping 的真实维度（properties.vector.dims）。
  注意：store 构造参数里的 num_dimensions 只是「建索引预期值」，索引已存在时
  永不回填——维度事实以 mapping 为准。
- drop_index(kb_id)：删库 = 删索引（维度变更/删库时调用；mapping 维度不可变）。

chunk metadata 约定：{kb_id, doc_id, filename, mime, chunk_index, page}；按
`metadata.doc_id.keyword` 过滤实现单文档删除/重建。distance_strategy=COSINE，
strategy=DenseVectorStrategy()；mapping 首次 add_documents 自动建（维度 = dims）。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化本工厂；嵌入客户端按 dims
构建并缓存（by-dims），保证 ingest 与 query 维度对称。
"""
from __future__ import annotations

import logging
from typing import Optional

from elasticsearch import Elasticsearch
from langchain_core.embeddings import Embeddings
from langchain_elasticsearch import (
    DenseVectorStrategy,
    DistanceStrategy,
    ElasticsearchStore,
)

from app.config import Settings
from app.services.embedding import build_embeddings

logger = logging.getLogger(__name__)


def _es_kwargs(settings: Settings) -> dict:
    """从 settings 提取 ES 认证参数（供 ElasticsearchStore 与原生 client 共用）。"""
    kw: dict = {"es_url": settings.es_url}
    if settings.es_api_key:
        kw["es_api_key"] = settings.es_api_key
    if settings.es_user:
        kw["es_user"] = settings.es_user
        kw["es_password"] = settings.es_password
    return kw


def _build_native_client(settings: Settings) -> Elasticsearch:
    """原生 ES 客户端（用于索引级管理：drop_index / get_dims 维度探测）。

    ElasticsearchStore 自建客户端只负责向量读写，索引管理与 mapping 探测走原生。
    """
    if settings.es_api_key:
        return Elasticsearch(settings.es_url, api_key=settings.es_api_key)
    if settings.es_user:
        return Elasticsearch(settings.es_url, basic_auth=(settings.es_user, settings.es_password))
    return Elasticsearch(settings.es_url)


class VectorStoreFactory:
    """按 (kb_id|索引名, dims) 缓存 ElasticsearchStore；持有原生 client 做索引管理。

    嵌入客户端按 dims 构建并缓存（同一维度复用一个 OpenAIEmbeddings 实例，
    避免重复建连）；dims=None 表示全局默认（settings.embedding_dims，可能仍为
    None=模型原生维度）。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._embeddings: dict[Optional[int], Embeddings] = {}
        self._by_kb: dict[tuple[str, Optional[int]], ElasticsearchStore] = {}
        self._by_index: dict[tuple[str, Optional[int]], ElasticsearchStore] = {}
        self._client = _build_native_client(settings)

    def _get_embeddings(self, dims: Optional[int]) -> Embeddings:
        em = self._embeddings.get(dims)
        if em is None:
            em = build_embeddings(self.settings, dims)
            self._embeddings[dims] = em
        return em

    def _strategy(self) -> DenseVectorStrategy:
        # COSINE 距离；hybrid/rrf 等增强留作后续（见计划 §十一）。
        return DenseVectorStrategy()

    def _build_store(self, index_name: str, dims: Optional[int]) -> ElasticsearchStore:
        kw = _es_kwargs(self.settings)
        # num_dimensions 与该库 dims 同源：让首次 add_documents 自动建的 mapping
        # 维度确定，避免探测歧义。None 时不传（由嵌入服务探测模型原生维度）。
        if dims is not None:
            kw["num_dimensions"] = dims
        return ElasticsearchStore(
            index_name=index_name,
            embedding=self._get_embeddings(dims),
            strategy=self._strategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get(self, kb_id: str, dims: Optional[int] = None) -> ElasticsearchStore:
        """单库 store（缓存 by (kb_id, dims)）。index_name = kb_<kb_id>。

        dims = kb.dims（None=全局默认）。维度是 store 构造的一部分：同库不同维度
        是两个不同缓存项（维度变更后旧 store 不复用，索引也已重建）。
        """
        key = (kb_id, dims)
        store = self._by_kb.get(key)
        if store is None:
            store = self._build_store(f"kb_{kb_id}", dims)
            self._by_kb[key] = store
        return store

    def get_for_index(self, index: str, dims: Optional[int] = None) -> ElasticsearchStore:
        """通配/多索引 store（缓存键=(index, dims)）。

        - index='kb_a,kb_b'：逗号拼接多库（须为同维度组，调用方分组后进入）。
        dims 决定查询向量维度，必须与组内各索引 mapping 一致。
        """
        key = (index, dims)
        store = self._by_index.get(key)
        if store is None:
            store = self._build_store(index, dims)
            self._by_index[key] = store
        return store

    def get_dims(self, kb_id: str) -> Optional[int]:
        """读索引 mapping 的真实维度（properties.vector.dims）。

        索引不存在（空库，从未入库）→ None。store.num_dimensions 只是构造预期值
        （索引已存在时永不回填），漂移检测/守卫须以本方法为准。
        """
        index_name = f"kb_{kb_id}"
        try:
            resp = self._client.indices.get_mapping(index=index_name)
            return resp[index_name]["mappings"]["properties"]["vector"]["dims"]
        except Exception:  # noqa: BLE001
            # 索引不存在（404）/ 无 vector 字段 → 空库，无真值
            return None

    def drop_index(self, kb_id: str) -> None:
        """删库 = 删索引 kb_<kb_id>（幂等：不存在不报错）。

        维度不可变 ⇒ 维度变更也走此方法（旧维度向量全部作废）。删除后失效该库
        所有维度的缓存项（维度变更前后可能各有一份）。
        """
        index_name = f"kb_{kb_id}"
        try:
            self._client.indices.delete(index=index_name, ignore_unavailable=True)
            logger.info("已删除 ES 索引 %s", index_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除 ES 索引 %s 失败: %s", index_name, e)
        # 失效缓存：单库缓存按 kb_id 前缀；多索引缓存键可能内含该索引名（逗号拼接）
        self._by_kb = {k: v for k, v in self._by_kb.items() if k[0] != kb_id}
        self._by_index = {
            k: v for k, v in self._by_index.items()
            if kb_id not in k[0].split(",")
        }

    def delete_by_doc_id(self, kb_id: str, doc_id: str) -> None:
        """删除某文档的全部 chunk（按 metadata.doc_id.keyword 过滤，幂等）。

        用于单文档删除/重建前的清旧：保证 ingest 幂等，无残留旧向量。
        """
        index_name = f"kb_{kb_id}"
        try:
            self._client.delete_by_query(
                index=index_name,
                query={"term": {"metadata.doc_id.keyword": str(doc_id)}},
                refresh=True,
                conflicts="proceed",  # 并发版本冲突时继续删除其余
            )
        except Exception as e:  # noqa: BLE001
            # 索引可能尚未创建（文档从未 ingest 成功）→ 视作无 chunk 可删，吞掉。
            logger.debug("delete_by_query %s doc=%s: %s", index_name, doc_id, e)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
