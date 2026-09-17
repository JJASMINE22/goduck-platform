"""向量化客户端工厂（知识库检索侧，query 嵌入）。

轻量拷贝自 backend/app/services/embedding.py（检索侧只需嵌入，不需入库）。
- 维度 per-KB：kb_search 按 ES mapping 真值分组后，各组以组维度构建查询向量；
  dims=None 时回落全局 EMBEDDING_DIMS（默认/兜底；仍为 None=模型原生维度）。
  backend(入库按 kb.dims) 与本侧(检索按 mapping 分组) 维度对称 → 同库
  ingest 与 query 维度恒等。agent 全程不碰维度。

- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url 正确。
- check_embedding_ctx_length 必须为 False（TEI 场景）：否则 langchain 用 tiktoken 按
  model 分词，对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
- dimensions 非 None 时由 TEI /v1/embeddings 服务端做 MRL 截断。

直连语义（trust_env=False，只作用于嵌入客户端）：
- TEI 位于局域网。Windows 上 httpx（trust_env）经 urllib.getproxies() 读取注册表
  系统代理，但不应用 ProxyOverride 绕过列表 ⇒ 宿主机开系统代理（Clash 等）时，
  嵌入请求会被劫持进代理，局域网目标不可达 → 502/超时（阵发性，随代理分流配置
  切换）。注入 trust_env=False 的 httpx 客户端使其绕过一切环境/系统代理直连 TEI。
- 其余出站（LLM、外部 MCP）不在此列，保持代理语义不受影响。
"""

from __future__ import annotations

from typing import Optional

import httpx
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


def build_embeddings(dims: Optional[int] = None) -> Embeddings:
    """构造嵌入客户端（TEI 经 OpenAI 兼容端点 / 真实 OpenAI 云端）。

    dims：组维度（来自 ES mapping 探测）；None 时回落全局 EMBEDDING_DIMS。

    嵌入客户端固定直连（不读环境/系统代理）：见模块 docstring「直连语义」。
    """
    actual_dims = dims if dims is not None else EMBEDDING_DIMS
    kwargs: dict = dict(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        check_embedding_ctx_length=EMBEDDING_CHECK_CTX_LENGTH,
        request_timeout=EMBEDDING_TIMEOUT,
        chunk_size=EMBEDDING_BATCH_SIZE,  # 单请求文本数，须 ≤ TEI max-client-batch-size
        # 局域网 TEI 必须绕过系统代理直连（模块 docstring）；同步/异步路径对称覆盖。
        # timeout 双保险：SDK 每请求会带 request_timeout 覆盖，这里兜底非 SDK 路径。
        http_client=httpx.Client(trust_env=False, timeout=EMBEDDING_TIMEOUT),
        http_async_client=httpx.AsyncClient(trust_env=False, timeout=EMBEDDING_TIMEOUT),
    )
    if actual_dims is not None:
        kwargs["dimensions"] = actual_dims
    return OpenAIEmbeddings(**kwargs)
