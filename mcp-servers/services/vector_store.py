"""Elasticsearch 向量库工厂（知识库检索侧，维度/模型感知）。

一库一索引：index_name = `kb_<kb_id>`。
- get_dims(kb_ids)：一次 get_mapping 逗号多索引探测 → {index_name: dims}。
  维度事实来源（store 构造参数 num_dimensions 只是预期值，索引已存在时永不
  回填）；索引不存在（空库）→ 不在返回值中（无 chunk 无命中，调用方跳过）。
- get_for_index(index, emb, dims)：按 (index, embedding 身份) 懒构建 + 缓存。
  - index='kb_a,kb_b'：逗号拼接多库（ES 原生多索引查询），须为同向量模型组。
  - emb=embedding 注册行（注册化主路径）：查询向量出自该行端点/维度；
    emb=None=legacy（dims + env，注册行缺失/PG 回落），行为=历史版本。
- distance_strategy=COSINE；mapping 由 backend 首写入时按注册行维度建。

缓存键含 embedding 身份而非仅 dims：同 dims 不同端点是不同向量空间，
by-dims 复用客户端会拿 A 端点查 B 模型索引（维度对、语义错、静默无报错）。
注册行客户端按 id 缓存：语义字段被 KB 引用后 backend 侧锁定不可变，
故按 id 缓存无悬挂风险（batch/timeout 微调至多重启生效，可接受）。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化；嵌入取值同源
（同一 embedding 注册行）→ ingest 与 query 向量空间恒等。
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from elasticsearch import Elasticsearch, NotFoundError
from langchain_core.embeddings import Embeddings
from langchain_elasticsearch import (
    DenseVectorStrategy,
    DistanceStrategy,
    ElasticsearchStore,
)

from config import ES_API_KEY, ES_PASSWORD, ES_URL, ES_USER
from services.embedding import build_embeddings, build_from_row

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


def _emb_key(emb: Optional[dict], dims: Optional[int]) -> str:
    """缓存键的 embedding 身份段：注册行 id / legacy dims。"""
    return f"e:{emb['id']}" if emb is not None else f"d:{dims}"


class VectorStoreFactory:
    """按 (索引名, embedding 身份) 懒构建 + 缓存 ElasticsearchStore。

    嵌入客户端双源缓存：注册行 by embedding_id / legacy by dims（env）。
    用户每次选的 kb_ids 组合不同 → index 名不同 → 按需创建并缓存。
    """

    def __init__(self):
        self._embeddings: dict[Optional[int], Embeddings] = {}      # legacy: by dims（env）
        self._row_embeddings: dict[uuid.UUID, Embeddings] = {}      # 注册行: by id
        self._cache: dict[tuple[str, str], ElasticsearchStore] = {}
        self._client = _build_native_client()

    def _client_for(self, emb: Optional[dict], dims: Optional[int]) -> tuple[Embeddings, Optional[int]]:
        """嵌入客户端 + 建索引预期维度：注册行优先，legacy 回落 env by-dims。"""
        if emb is not None:
            em = self._row_embeddings.get(emb["id"])
            if em is None:
                em = build_from_row(emb)
                self._row_embeddings[emb["id"]] = em
            return em, emb.get("dims")
        em = self._embeddings.get(dims)
        if em is None:
            em = build_embeddings(dims)
            self._embeddings[dims] = em
        return em, dims

    def _build_store(
        self, index_name: str, dims: Optional[int], embeddings: Embeddings
    ) -> ElasticsearchStore:
        kw = _es_kwargs()
        if dims is not None:
            kw["num_dimensions"] = dims
        return ElasticsearchStore(
            index_name=index_name,
            embedding=embeddings,
            strategy=DenseVectorStrategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get_for_index(
        self, index: str, emb: Optional[dict] = None, dims: Optional[int] = None
    ) -> ElasticsearchStore:
        """同向量模型组 store（懒构建 + 缓存键=(index, embedding 身份)）。

        - index='kb_a,kb_b'：逗号拼接多库（调用方按注册行/维度分组后进入）。
        - emb=注册行（dims 入参被忽略——行维度为真值）；None=legacy（dims+env）。
        """
        key = (index, _emb_key(emb, dims))
        store = self._cache.get(key)
        if store is None:
            embeddings, eff_dims = self._client_for(emb, dims)
            store = self._build_store(index, eff_dims, embeddings)
            self._cache[key] = store
            logger.info("JIT 构建 ElasticsearchStore: index=%s emb=%s", index, key[1])
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
