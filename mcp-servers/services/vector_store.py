"""Elasticsearch 向量库工厂（知识库检索侧，JIT store）。

轻量拷贝自 backend/app/services/vector_store.py（检索侧只需 get_for_index）。

一库一索引：index_name = `kb_<kb_id>`。
- get_for_index(index)：按索引名懒构建 + 缓存 ElasticsearchStore（JIT，不预载）。
  - index='kb_*'：检索全部 KB。
  - index='kb_a,kb_b'：逗号拼接多库（ES 原生多索引查询）。
- 全局单维（EMBEDDING_DIMS）⇒ 各库同维 ⇒ 跨索引/通配查询安全。
- distance_strategy=COSINE；mapping 维度 = EMBEDDING_DIMS（首写入时由 ES 自动建）。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化、读同一份 EMBEDDING_DIMS。
"""
from __future__ import annotations

import logging

from langchain_core.embeddings import Embeddings
from langchain_elasticsearch import (
    DenseVectorStrategy,
    DistanceStrategy,
    ElasticsearchStore,
)

from config import ES_API_KEY, ES_PASSWORD, ES_URL, ES_USER, EMBEDDING_DIMS

logger = logging.getLogger(__name__)


def _es_kwargs() -> dict:
    """提取 ES 认证参数（仅非空项），供 ElasticsearchStore 共用。"""
    kw: dict = {"es_url": ES_URL}
    if ES_API_KEY:
        kw["es_api_key"] = ES_API_KEY
    if ES_USER:
        kw["es_user"] = ES_USER
        kw["es_password"] = ES_PASSWORD
    return kw


class VectorStoreFactory:
    """按索引名懒构建 + 缓存 ElasticsearchStore（JIT，不预载全部 ES 对象）。

    用户每次选的 kb_ids 组合不同 → index 名不同 → 按需创建并缓存，
    避免启动时预载所有库（数量不可控、且大多用不到）。
    """

    def __init__(self, embeddings: Embeddings):
        self.embeddings = embeddings
        self._cache: dict[str, ElasticsearchStore] = {}

    def _build_store(self, index_name: str) -> ElasticsearchStore:
        kw = _es_kwargs()
        # num_dimensions 与 embedding 客户端 dimensions 同源（全局 EMBEDDING_DIMS）：
        # 让 mapping 维度确定，避免探测歧义。
        if EMBEDDING_DIMS is not None:
            kw["num_dimensions"] = EMBEDDING_DIMS
        return ElasticsearchStore(
            index_name=index_name,
            embedding=self.embeddings,
            strategy=DenseVectorStrategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get_for_index(self, index: str) -> ElasticsearchStore:
        """通配/多索引 store（懒构建 + 缓存键=index）。

        - index='kb_*'：检索全部 KB（kb_ids=None 场景）。
        - index='kb_a,kb_b'：逗号拼接多库。
        全局单维 ⇒ 各库同维 ⇒ 跨索引/通配查询安全。
        """
        store = self._cache.get(index)
        if store is None:
            store = self._build_store(index)
            self._cache[index] = store
            logger.info("JIT 构建 ElasticsearchStore: index=%s", index)
        return store
