"""向量化客户端工厂（知识库 RAG，入库 / 检索共用）。

双路径（plan: kb-embedding-model-registry Phase 1 双读）：
- 注册化：embedding 表行驱动（``build_from_row``），端点/凭据/维度/批大小/
  超时/ctx 校验全部随行走——多向量模型并存的主路径。
- legacy：env 全局单例（``build_embeddings``），迁移期回落（kb.embedding_id
  悬空 / mcp-servers 未切换），行为与历史版本一致。

- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url
  正确且含 /v1（见 demo/agents/embeddings.py）。
- check_embedding_ctx_length 须为 False（TEI 场景）：否则 langchain 用
  tiktoken 按 model 分词，对 TEI 部署的模型会报错。真实 OpenAI 云端设 True。
- dims：MRL 截断目标（如 Qwen3-Embedding 原生 1024 → 512）；None=模型原生
  维度（不传 dimensions）。同一库 ingest 与 query 同值（对称）→ ES mapping
  维度 == query 向量维度。

直连语义（trust_env=False，只作用于嵌入客户端）：
- 嵌入端点位于局域网/compose 内网。Windows 上 httpx（trust_env）经
  urllib.getproxies() 读取注册表系统代理，但不应用 ProxyOverride 绕过列表
  ⇒ 宿主机开系统代理（Clash 等）时，嵌入请求会被劫持进代理，局域网目标
  不可达 → 502/超时。注入 trust_env=False 的 httpx 客户端绕过一切环境/系统
  代理直连。其余出站（LLM、外部 MCP）不在此列，保持代理语义。
"""

from __future__ import annotations

from typing import Optional

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import Settings


def _openai_client(
    *,
    model: str,
    api_key: str,
    base_url: str,
    dims: Optional[int],
    batch_size: int,
    timeout: int,
    check_ctx_length: bool,
) -> OpenAIEmbeddings:
    """公共构造（两路径唯一汇合点；直连语义见模块 docstring）。"""
    kwargs: dict = dict(
        model=model,
        api_key=api_key,
        base_url=base_url,
        check_embedding_ctx_length=check_ctx_length,
        request_timeout=timeout,
        chunk_size=batch_size,  # 单请求文本数，须 ≤ TEI max-client-batch-size（默认32）
        # timeout 双保险：SDK 每请求会带 request_timeout 覆盖，这里兜底非 SDK 路径。
        http_client=httpx.Client(trust_env=False, timeout=timeout),
        http_async_client=httpx.AsyncClient(trust_env=False, timeout=timeout),
    )
    if dims is not None:
        kwargs["dimensions"] = dims
    return OpenAIEmbeddings(**kwargs)


def build_from_row(row: dict) -> Embeddings:
    """按 embedding 注册行构建客户端（注册化主路径；参数随行走）。"""
    return _openai_client(
        model=row["model_type"],
        api_key=row["api_key"],
        base_url=row["base_url"],
        dims=row.get("dims"),
        batch_size=row.get("batch_size") or 24,
        timeout=row.get("timeout") or 90,
        check_ctx_length=bool(row.get("check_ctx_length")),
    )


def build_embeddings(settings: Settings, dims: Optional[int] = None) -> Embeddings:
    """legacy env 路径（迁移期回落）：dims 缺省回落全局 settings.embedding_dims。"""
    return _openai_client(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        dims=dims if dims is not None else settings.embedding_dims,
        batch_size=settings.embedding_batch_size,
        timeout=settings.embedding_timeout,
        check_ctx_length=settings.embedding_check_ctx_length,
    )
