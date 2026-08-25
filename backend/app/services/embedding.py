"""向量化客户端工厂（知识库 RAG，入库 / 检索共用）。

单一路径：经 OpenAIEmbeddings 指向 TEI 的 OpenAI 兼容端点（/v1）。
- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url 正确
  （见 demo/agents/embeddings.py）。
- check_embedding_ctx_length 必须为 False（TEI 场景）：否则 langchain 用 tiktoken 按
  model 分词，对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
- dimensions：per-KB 维度（kb.dims，缺省回落全局 settings.embedding_dims）。
  MRL 截断（Qwen3-Embedding 原生 1024 → 512）；同一 kb 的 ingest 与 query 同值
  （对称）→ ES 索引 mapping 维度 == query 向量维度。

backend(入库) 与 mcp-servers(检索) 各自实例化本工厂；维度取值源均为 kb.dims
（缺省回落各自的全局 EMBEDDING_DIMS）→ 两边 dimensions 对称。agent 全程不碰维度。
"""
from __future__ import annotations

from typing import Optional

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import Settings


def build_embeddings(settings: Settings, dims: Optional[int] = None) -> Embeddings:
    """构造嵌入客户端（TEI 经 OpenAI 兼容端点 / 真实 OpenAI 云端）。

    dims：per-KB 维度；None 时回落全局 settings.embedding_dims（模型原生维度时
    为 None，即不传 dimensions）。非 None 时由 TEI /v1/embeddings 服务端做 MRL 截断。
    """
    actual_dims = dims if dims is not None else settings.embedding_dims
    kwargs: dict = dict(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        check_embedding_ctx_length=settings.embedding_check_ctx_length,
        request_timeout=settings.embedding_timeout,
        chunk_size=settings.embedding_batch_size,  # 单请求文本数，须 ≤ TEI max-client-batch-size（默认32）
    )
    if actual_dims is not None:
        kwargs["dimensions"] = actual_dims
    return OpenAIEmbeddings(**kwargs)
