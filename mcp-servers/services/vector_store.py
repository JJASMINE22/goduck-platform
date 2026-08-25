"""Elasticsearch 向量库工厂（知识库检索侧，JIT store + 维度感知）。

轻量拷贝自 backend/app/services/vector_store.py（检索侧只需 get_for_index/get_dims）。

一库一索引：index_name = `kb_<kb_id>`。
- get_dims(kb_ids)：一次 get_mapping 逗号多索引探测 → {index_name: dims}。
  维度事实来源（store 构造参数 num_dimensions 只是预期值，索引已存在时永不回填）；
  索引不存在（空库）→ 不在返回值中（无 chunk 无命中，调用方跳过）。
- get_for_index(index, dims)：按 (index, dims) 懒构建 + 缓存 ElasticsearchStore。
  - index='kb_a,kb_b'：逗号拼接多库（ES 原生多索引查询），须为同维度组。
  - dims 决定查询向量维度（嵌入客户端按 dims 构建 + 缓存），与组内 mapping 一致。
- distance_strategy=COSINE；mapping 由 backend 首写入时按 kb.dims 建。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化；嵌入维度取值源对称
（backend=kb.dims 行记录，本侧=ES mapping 真值 → 同一索引两者恒等）。
"""
from __future__ import annotations

import logging
from typing import Optional

from elasticsearch import Elasticsearch, NotFoundError
from langchain_core.embeddings import Embeddings
from langchain_elasticsearch import (
    DenseVectorStrategy,
    DistanceStrategy,
    ElasticsearchStore,
)

from config import ES_API_KEY, ES_PASSWORD, ES_URL, ES_USER
from services.embedding import build_embeddings

logger = logging.getLogger(__name__)


def _es_kwargs() -> dict:
    """提取 ES 认证参数（仅非空项），供 ElasticsearchStore 与原生 client 共用。"""
    kw: dict = {"es_url": ES_URL}
    if ES_API_KEY:
        kw["es_api_key"] = ES_API_KEY
    if ES_USER:
        kw["es_user"] = ES_USER
        kw["es_password"] = ES_PASSWORD
    return kw


def _build_native_client() -> Elasticsearch:
    """原生 ES 客户端：mapping 维度探测（get_dims）专用。

    独立于 JIT store——探测不应要求 store 已构建；仅多一个连接。
    """
    if ES_API_KEY:
        return Elasticsearch(ES_URL, api_key=ES_API_KEY)
    if ES_USER:
        return Elasticsearch(ES_URL, basic_auth=(ES_USER, ES_PASSWORD))
    return Elasticsearch(ES_URL)


class VectorStoreFactory:
    """按 (索引名, dims) 懒构建 + 缓存 ElasticsearchStore；按 dims 缓存嵌入客户端。

    用户每次选的 kb_ids 组合不同 → index 名不同 → 按需创建并缓存，
    避免启动时预载所有库（数量不可控、且大多用不到）。维度进入缓存键：
    同一 index 组合在不同维度下是不同的查询向量空间。
    """

    def __init__(self):
        self._embeddings: dict[Optional[int], Embeddings] = {}
        self._cache: dict[tuple[str, Optional[int]], ElasticsearchStore] = {}
        self._client = _build_native_client()

    def _get_embeddings(self, dims: Optional[int]) -> Embeddings:
        em = self._embeddings.get(dims)
        if em is None:
            em = build_embeddings(dims)
            self._embeddings[dims] = em
        return em

    def _build_store(self, index_name: str, dims: Optional[int]) -> ElasticsearchStore:
        kw = _es_kwargs()
        # num_dimensions 与组 dims 同源：查询向量维度与组内各索引 mapping 一致。
        # None 时不传（模型原生维度场景，由嵌入服务保证对称）。
        if dims is not None:
            kw["num_dimensions"] = dims
        return ElasticsearchStore(
            index_name=index_name,
            embedding=self._get_embeddings(dims),
            strategy=DenseVectorStrategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get_for_index(self, index: str, dims: Optional[int] = None) -> ElasticsearchStore:
        """同维度组 store（懒构建 + 缓存键=(index, dims)）。

        - index='kb_a,kb_b'：逗号拼接多库（调用方按 mapping dims 分组后进入）。
        """
        key = (index, dims)
        store = self._cache.get(key)
        if store is None:
            store = self._build_store(index, dims)
            self._cache[key] = store
            logger.info("JIT 构建 ElasticsearchStore: index=%s dims=%s", index, dims)
        return store

    def get_dims(self, kb_ids: list[str]) -> dict[str, int]:
        """一次 round-trip 探测所选各库的真实维度 → {index_name(kb_<id>): dims}。

        - 索引未建（上传未解析/空库）→ 不在返回值中（调用方按未命中处理）；
          全部缺失时返回空 dict（含 ES 对全缺失返回 404 的情形，归一为空）。
        - 索引存在但无 vector 字段（异常数据）→ 跳过并告警。
        - 传输层失败（ES 不可达等）→ 抛出异常：调用方需与「空结果」区分处理
          （不可达是故障，空库是正常业务态）。
        """
        if not kb_ids:
            return {}
        names = [f"kb_{kid}" for kid in kb_ids]
        try:
            resp = self._client.indices.get_mapping(
                index=",".join(names), ignore_unavailable=True
            )
        except NotFoundError:
            # ignore_unavailable 下全部索引缺失仍可能以 404 表达 → 归一为空 dict
            return {}
        except Exception as e:  # noqa: BLE001
            logger.warning("get_dims 探测失败 kb_ids=%s: %s", kb_ids, e)
            raise
        out: dict[str, int] = {}
        for index_name, body in resp.items():
            try:
                dims = body["mappings"]["properties"]["vector"]["dims"]
            except (KeyError, TypeError):
                logger.warning("索引 %s 缺少 vector.dims mapping，跳过", index_name)
                continue
            if isinstance(dims, int):
                out[index_name] = dims
        return out
