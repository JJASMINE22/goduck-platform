"""向量化客户端工厂（知识库检索侧，query 嵌入）。

轻量拷贝自 backend/app/services/embedding.py（检索侧只需嵌入，不需入库）。
backend(入库) 与 mcp-servers(检索) 各自实例化、读同一份 EMBEDDING_DIMS →
两边 dimensions 自动相等。agent 全程不碰维度。

- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url 正确。
- check_embedding_ctx_length 必须为 False（TEI 场景）：否则 langchain 用 tiktoken 按
  model 分词，对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
- dimensions：全局唯一来源（config.EMBEDDING_DIMS）。MRL 截断（如 1024→512）；
  ingest 与 query 同值（对称）→ ES 索引 mapping 维度 == query 向量维度。
"""
from __future__ import annotations

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_CHECK_CTX_LENGTH,
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    EMBEDDING_TIMEOUT,
)


def build_embeddings() -> Embeddings:
    """构造嵌入客户端（TEI 经 OpenAI 兼容端点 / 真实 OpenAI 云端）。

    dimensions 为全局单一来源：None 时不传（用模型原生维度）；非 None 时由 TEI
    /v1/embeddings 服务端做 MRL 截断。
    """
    kwargs: dict = dict(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        check_embedding_ctx_length=EMBEDDING_CHECK_CTX_LENGTH,
        request_timeout=EMBEDDING_TIMEOUT,
        chunk_size=EMBEDDING_BATCH_SIZE,  # 单请求文本数，须 ≤ TEI max-client-batch-size
    )
    if EMBEDDING_DIMS is not None:
        kwargs["dimensions"] = EMBEDDING_DIMS
    return OpenAIEmbeddings(**kwargs)
