"""Elasticsearch 向量库工厂（知识库 RAG）。

一库一索引：index_name = `kb_<kb_id>`。
- get(kb_id, emb, dims)：按 (kb_id, embedding 身份) 缓存单库 store（写路径）。
  emb=embedding 注册行（注册化主路径，客户端出自 EmbeddingStore）；
  emb=None 走 legacy（dims + env，迁移期回落——行为与历史版本一致）。
- get_for_index(index, emb, dims)：通配/多索引 store（index='kb_a,kb_b'，仅限
  同向量模型组，调用方分组后进入）；读路径跨库检索。
- get_dims(kb_id)：读 ES mapping 的真实维度（properties.vector.dims）。
  注意：store 构造参数里的 num_dimensions 只是「建索引预期值」，索引已存在时
  永不回填——维度事实以 mapping 为准（漂移检测防线，不可删）。
- drop_index(kb_id)：删库 = 删索引（换绑/删库时调用；mapping 维度不可变）。
- invalidate_embedding(embedding_id)：注册行变更后失效其派生 store 缓存
  （客户端由 EmbeddingStore.refresh 换血，此处清 store 侧引用）。

缓存键含 embedding 身份而非仅 dims：同 dims 不同端点的两个模型是不同向量
空间，by-dims 会拿 A 端点客户端查 B 模型索引（维度对、语义错、静默无报错）。

chunk metadata 约定：{kb_id, doc_id, filename, mime, chunk_index, page}；按
`metadata.doc_id.keyword` 过滤实现单文档删除/重建。distance_strategy=COSINE，
strategy=DenseVectorStrategy()；mapping 首次 add_documents 自动建（维度 = dims）。

backend(入库/管理) 与 mcp-servers(检索) 各自实例化本工厂；注册化路径两侧
经 embedding 注册行对齐（mcp-servers 对齐属 Phase 2，当前仍 env+dims）。
"""
from __future__ import annotations

import logging
import uuid
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

# docling 结构单元字段（B3）：既有索引 live 增列（put_mapping 增量）。
# 纯增量契约——dense_vector 不动、无 reindex、不停写；legacy 单元缺省为 null
# （数据双轨期）。bbox/assets 只进 _source 不索引（object enabled:false）。
# 逐字段独立 put_mapping：单字段类型冲突（早期动态映射先建了 text 之类）
# 只降级该字段并告警，不阻塞其余字段与入库（lang/整型过滤 v1 不依赖）。
_DOCLING_FIELDS: dict[str, dict] = {
    "heading_path": {"type": "text"},
    "text_ctx": {"type": "text"},
    "kind": {"type": "keyword"},
    "lang": {"type": "keyword"},
    "doc_uri": {"type": "keyword"},
    "page_rotate": {"type": "integer"},
    "bbox": {"type": "object", "enabled": False},
    "assets": {"type": "object", "enabled": False},
}


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


def _emb_key(emb: Optional[dict], dims: Optional[int]) -> str:
    """缓存键的 embedding 身份段：注册行 id / legacy dims。"""
    return f"e:{emb['id']}" if emb is not None else f"d:{dims}"


class VectorStoreFactory:
    """按 (kb_id|索引名, embedding 身份) 缓存 ElasticsearchStore；持有原生
    client 做索引管理。

    嵌入客户端双源：注册行 → EmbeddingStore（by embedding_id）；legacy →
    env by-dims（迁移期回落）。embedding_store 由 main 装配注入。
    """

    def __init__(self, settings: Settings, embedding_store=None):
        self.settings = settings
        self.embedding_store = embedding_store
        self._embeddings: dict[Optional[int], Embeddings] = {}       # legacy: by dims（env）
        self._by_kb: dict[tuple[str, str], ElasticsearchStore] = {}  # (kb_id, emb_key)
        self._by_index: dict[tuple[str, str], ElasticsearchStore] = {}
        self._client = _build_native_client(settings)

    def _client_for(self, emb: Optional[dict], dims: Optional[int]) -> tuple[Embeddings, Optional[int]]:
        """嵌入客户端 + 建索引预期维度：注册行优先，legacy 回落 env by-dims。"""
        if emb is not None:
            return self.embedding_store.client(emb["id"]), emb.get("dims")
        em = self._embeddings.get(dims)
        if em is None:
            em = build_embeddings(self.settings, dims)
            self._embeddings[dims] = em
        return em, dims

    def _strategy(self) -> DenseVectorStrategy:
        # COSINE 距离；hybrid/rrf 等增强留作后续（见计划 §十一）。
        return DenseVectorStrategy()

    def _build_store(
        self, index_name: str, dims: Optional[int], embeddings: Embeddings
    ) -> ElasticsearchStore:
        kw = _es_kwargs(self.settings)
        # num_dimensions 与该库维度同源：让首次 add_documents 自动建的 mapping
        # 维度确定。None 时不传（模型原生维度，由嵌入服务保证对称）。
        if dims is not None:
            kw["num_dimensions"] = dims
        return ElasticsearchStore(
            index_name=index_name,
            embedding=embeddings,
            strategy=self._strategy(),
            distance_strategy=DistanceStrategy.COSINE,
            **kw,
        )

    def get(
        self,
        kb_id: str,
        emb: Optional[dict] = None,
        dims: Optional[int] = None,
    ) -> ElasticsearchStore:
        """单库 store（缓存 by (kb_id, embedding 身份)）。index_name = kb_<kb_id>。

        emb=注册行（注册化路径，dims 入参被忽略——行维度为真值）；
        emb=None=legacy（dims + env，kb.embedding_id 悬空的迁移期回落）。
        """
        key = (kb_id, _emb_key(emb, dims))
        store = self._by_kb.get(key)
        if store is None:
            embeddings, eff_dims = self._client_for(emb, dims)
            store = self._build_store(f"kb_{kb_id}", eff_dims, embeddings)
            self._by_kb[key] = store
        return store

    def get_for_index(
        self, index: str, emb: Optional[dict] = None, dims: Optional[int] = None
    ) -> ElasticsearchStore:
        """通配/多索引 store（缓存键=(index, embedding 身份)）。

        - index='kb_a,kb_b'：逗号拼接多库（须为同向量模型组，调用方分组后进入）。
        组内查询向量维度与各索引 mapping 一致。
        """
        key = (index, _emb_key(emb, dims))
        store = self._by_index.get(key)
        if store is None:
            embeddings, eff_dims = self._client_for(emb, dims)
            store = self._build_store(index, eff_dims, embeddings)
            self._by_index[key] = store
        return store

    def invalidate_embedding(self, embedding_id: uuid.UUID) -> None:
        """注册行变更后失效其派生 store（客户端随 EmbeddingStore.refresh 换血）。"""
        tag = f"e:{embedding_id}"
        self._by_kb = {k: v for k, v in self._by_kb.items() if k[1] != tag}
        self._by_index = {k: v for k, v in self._by_index.items() if k[1] != tag}

    def get_dims(self, kb_id: str) -> Optional[int]:
        """读索引 mapping 的真实维度（properties.vector.dims）。

        索引不存在（空库，从未入库）→ None。store.num_dimensions 只是构造预期值
        （索引已存在时永不回填），漂移检测/守卫须以本方法为准。
        """
        index_name = f"kb_{kb_id}"
        try:
            resp = self._client.indices.get_mapping(index=index_name)
            return resp[index_name]["mappings"]["properties"]["vector"]["dims"]
        except Exception:
            # 索引不存在（404）/ 无 vector 字段 → 空库，无真值
            return None

    def ensure_docling_fields(self, kb_id: str) -> None:
        """给既有索引 live 增列 docling 字段（幂等；索引不存在 → 静默跳过）。

        调用时机（docling 入库路径）：写前调一次（覆盖既有索引）；索引由
        langchain 首批 add_documents 自动创建的场景，首批成功后再补调一次。
        首批动态映射可能先建出 text 型 lang/doc_uri——put_mapping 冲突即保持
        动态类型（检索出口从 _source 取值，不受 mapping 类型影响）。
        """
        index_name = f"kb_{kb_id}"
        try:
            resp = self._client.indices.get_mapping(index=index_name)
            existing = (
                resp[index_name].get("mappings", {}).get("properties", {}).get("metadata", {})
            )
            existing_sub = existing.get("properties", {}) if isinstance(existing, dict) else {}
        except Exception:
            return  # 索引不存在（空库）→ 无可增列
        for field_name, spec in _DOCLING_FIELDS.items():
            if field_name in existing_sub:
                continue  # 幂等：已显式映射（动态映射先建的字段也跳过，避免冲突）
            try:
                self._client.indices.put_mapping(
                    index=index_name,
                    body={"properties": {"metadata": {"properties": {field_name: spec}}}},
                )
            except Exception as e:
                logger.warning(
                    "kb=%s 增列 metadata.%s 失败（保持动态映射，不影响入库）: %s",
                    kb_id, field_name, e,
                )

    def get_chunk_source(self, kb_id: str, chunk_id: str) -> Optional[dict]:
        """按确定性 chunk id（``{doc_id}_{chunk_index}``，两引擎同构）读单个
        chunk 的 ``_source``（text/metadata，不含向量字段也可用）。

        索引不存在 / chunk 缺失 → None（聊天侧锚定卡按空位降级，非故障）。
        供 knowledge_service.anchor_cards（kb_search 工具结果的锚定补全）。
        """
        try:
            resp = self._client.get(index=f"kb_{kb_id}", id=chunk_id)
            source = resp.get("_source")
            return source if isinstance(source, dict) else None
        except Exception:
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
        except Exception as e:
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
        except Exception as e:
            # 索引可能尚未创建（文档从未 ingest 成功）→ 视作无 chunk 可删，吞掉。
            logger.debug("delete_by_query %s doc=%s: %s", index_name, doc_id, e)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
