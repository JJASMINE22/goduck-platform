"""向量化客户端工厂（知识库 RAG，入库 / 检索共用）。

单一路径：经 OpenAIEmbeddings 指向 TEI 的 OpenAI 兼容端点（/v1）。
- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url 正确
  （见 demo/agents/embeddings.py）。
- check_embedding_ctx_length 必须为 False（TEI 场景）：否则 langchain 用 tiktoken 按
  model 分词，对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
- dimensions：per-KB 维度（kb.dims，缺省回落全局 settings.embedding_dims）。
  MRL 截断（Qwen3-Embedding 原生 1024 → 512）；同一 kb 的 ingest 与 query 同值
  （对称）→ ES 索引 mapping 维度 == query 向量维度。

直连语义（trust_env=False，只作用于嵌入客户端）：
- TEI 位于局域网。Windows 上 httpx（trust_env）经 urllib.getproxies() 读取注册表
  系统代理，但不应用 ProxyOverride 绕过列表 ⇒ 宿主机开系统代理（Clash 等）时，
  嵌入请求会被劫持进代理，局域网目标不可达 → 502/超时（阵发性，随代理分流配置
  切换）。注入 trust_env=False 的 httpx 客户端使其绕过一切环境/系统代理直连 TEI。
- 其余出站（LLM、外部 MCP）不在此列，保持代理语义不受影响。

backend(入库) 与 mcp-servers(检索) 各自实例化本工厂；维度取值源均为 kb.dims
（缺省回落各自的全局 EMBEDDING_DIMS）→ 两边 dimensions 对称。agent 全程不碰维度。
"""

from __future__ import annotations

from typing import Optional

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import Settings


def build_embeddings(settings: Settings, dims: Optional[int] = None) -> Embeddings:
    """构造嵌入客户端（TEI 经 OpenAI 兼容端点 / 真实 OpenAI 云端）。

    dims：per-KB 维度；None 时回落全局 settings.embedding_dims（模型原生维度时
    为 None，即不传 dimensions）。非 None 时由 TEI /v1/embeddings 服务端做 MRL 截断。

    嵌入客户端固定直连（不读环境/系统代理）：见模块 docstring「直连语义」。
    """
    actual_dims = dims if dims is not None else settings.embedding_dims
    kwargs: dict = dict(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        check_embedding_ctx_length=settings.embedding_check_ctx_length,
        request_timeout=settings.embedding_timeout,
        chunk_size=settings.embedding_batch_size,  # 单请求文本数，须 ≤ TEI max-client-batch-size（默认32）
        # 局域网 TEI 必须绕过系统代理直连（模块 docstring）；同步/异步路径对称覆盖。
        # timeout 双保险：SDK 每请求会带 request_timeout 覆盖，这里兜底非 SDK 路径。
        http_client=httpx.Client(trust_env=False, timeout=settings.embedding_timeout),
        http_async_client=httpx.AsyncClient(trust_env=False, timeout=settings.embedding_timeout),
    )
    if actual_dims is not None:
        kwargs["dimensions"] = actual_dims
    return OpenAIEmbeddings(**kwargs)
