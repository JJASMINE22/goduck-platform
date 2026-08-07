"""Elasticsearch 向量库工厂（知识库 RAG）。

一库一索引：index_name = `kb_<kb_id>`。
- get(kb_id)：按 kb_id 缓存单库 store（写路径：入库/删文档）。
- get_for_index(index)：通配/多索引 store（index='kb_*' 或 'kb_a,kb_b'）；读路径
  跨库检索（全局单嵌入 ⇒ 各库同维 ⇒ 跨索引/通配查询安全）。
- drop_index(kb_id)：删库 = 删索引。

chunk metadata 约定：{kb_id, doc_id, filename, mime, chunk_index, page}；按
`metadata.doc_id.keyword` 过滤实现单文档删除/重建。distance_strategy=COSINE，
strategy=DenseVectorStrategy()；mapping 首次 add_documents 自动建（维度 = EMBEDDING_DIMS）。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化本工厂、读同一份 EMBEDDING_DIMS。
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

logger = logging.getLogger(__name__)


def _es_kwargs(settings: Settings) -> dict:
    """从 settings 提取 ES 认证参数（供 ElasticsearchStore 与原生 client 共用）。"""
    kw: dict = {"es_url": settings.es_url}
    if settings.es_api_key:
        kw["es_api_key"] = settings.es_api_key
    if settings.es_user:
        kw["es_user"] = settings.es_user
    if settings.es_password:
        kw["es_password"] = settings.es_password
    return kw


def _build_native_client(settings: Settings) -> Elasticsearch:
    """原生 ES 客户端（用于索引级管理：drop_index）。

    ElasticsearchStore 自建客户端只负责向量读写，删库走原生 indices.delete。
    """
    if settings.es_api_key:
        return Elasticsearch(settings.es_url, api_key=settings.es_api_key)
    if settings.es_user:
        return Elasticsearch(settings.es_url, basic_auth=(settings.es_user, settings.es_password))
    return Elasticsearch(settings.es_url)


class VectorStoreFactory:
    """按 kb_id / 索引名缓存 ElasticsearchStore；持有原生 client 做索引管理。"""

    def __init__(self, settings: Settings, embeddings: Embeddings):
        self.settings = settings
        self.embeddings = embeddings
        self._by_kb: dict[str, ElasticsearchStore] = {}
        self._by_index: dict[str, ElasticsearchStore] = {}
        self._client = _build_native_client(settings)

    def _strategy(self) -> DenseVectorStrategy:
        # COSINE 距离；hybrid/rrf 等增强留作后续（见计划 §十一）。
        return DenseVectorStrategy()

    def _build_store(self, index_name: str) -> ElasticsearchStore:
        kw = _es_kwargs(self.settings)
        # num_dimensions 与 embedding 客户端 dimensions 同源（全局 EMBEDDING_DIMS）：
        # 让首次 add_documents 自动建的 mapping 维度确定，避免探测歧义。
        if self.settings.embedding_dims is not None:
            kw["num_dimensions"] = self.settings.embedding_dims
        return ElasticsearchStore(
            index_name=index_name,
            embedding=self.embeddings,
            strategy=self._strategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get(self, kb_id: str, dims: Optional[int] = None) -> ElasticsearchStore:
        """单库 store（缓存 by kb_id）。index_name = kb_<kb_id>。

        dims 仅用于记录/校验（漂移检测由 KnowledgeService 层负责），此处忽略——
        索引维度由全局 EMBEDDING_DIMS 决定，与建库时记录的 dims 应一致。
        """
        store = self._by_kb.get(kb_id)
        if store is None:
            store = self._build_store(f"kb_{kb_id}")
            self._by_kb[kb_id] = store
        return store

    def get_for_index(self, index: str) -> ElasticsearchStore:
        """通配/多索引 store（缓存键=index）。

        - index='kb_*'：检索全部 KB（kb_ids=None 场景）。
        - index='kb_a,kb_b'：逗号拼接多库。
        全局单维 ⇒ 各库同维 ⇒ 跨索引/通配查询安全。
        """
        store = self._by_index.get(index)
        if store is None:
            store = self._build_store(index)
            self._by_index[index] = store
        return store

    def drop_index(self, kb_id: str) -> None:
        """删库 = 删索引 kb_<kb_id>（幂等：不存在不报错）。"""
        index_name = f"kb_{kb_id}"
        try:
            self._client.indices.delete(index=index_name, ignore_unavailable=True)
            logger.info("已删除 ES 索引 %s", index_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("删除 ES 索引 %s 失败: %s", index_name, e)
        # 失效缓存
        self._by_kb.pop(kb_id, None)
        self._by_index.pop(index_name, None)

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
