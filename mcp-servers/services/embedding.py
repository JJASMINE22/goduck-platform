"""向量化客户端工厂（知识库检索侧，query 嵌入）。

双路径（plan kb-embedding-model-registry Phase 2）：
- 注册化：embedding 表行驱动（``build_from_row``），与 backend 入库侧同源
  对称（同一注册行 ⇒ 同一端点/维度/参数 ⇒ 同一向量空间）。
- legacy：env 全局单例（``build_embeddings``），注册行缺失 / PG 不可达时
  回落，行为与历史版本一致。

- 本地 TEI：部署时已指定模型 ⇒ model/api_key 任意（TEI 忽略），只要 base_url
  正确且含 /v1。
- check_embedding_ctx_length 必须为 False（TEI 场景）：否则 langchain 用
  tiktoken 按 model 分词，对 TEI 部署的模型会报错。真实 OpenAI 云端设 True。
- dims 非 None 时由端点做 MRL 截断（dimensions 参数）。

直连语义（trust_env=False，只作用于嵌入客户端）：
- 嵌入端点位于局域网/compose 内网。Windows 上 httpx（trust_env）经
  urllib.getproxies() 读取注册表系统代理，但不应用 ProxyOverride 绕过列表
  ⇒ 宿主机开系统代理（Clash 等）时，嵌入请求会被劫持进代理，局域网目标
  不可达 → 502/超时。注入 trust_env=False 的 httpx 客户端绕过一切环境/系统
  代理直连。其余出站不在此列，保持代理语义。
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
        chunk_size=batch_size,
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


def build_embeddings(dims: Optional[int] = None) -> Embeddings:
    """legacy env 路径（回落）：dims 缺省回落全局 EMBEDDING_DIMS。"""
    return _openai_client(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
        dims=dims if dims is not None else EMBEDDING_DIMS,
        batch_size=EMBEDDING_BATCH_SIZE,
        timeout=EMBEDDING_TIMEOUT,
        check_ctx_length=EMBEDDING_CHECK_CTX_LENGTH,
    )
