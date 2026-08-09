"""集中配置加载：.env + mcp_servers.json。

env 字段中的 ${VAR} 占位符会从 .env 注入实际值。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_PATH = PROJECT_ROOT / ".env"
_JSON_PATH = PROJECT_ROOT / "mcp_servers.json"

# 启动即加载 .env
load_dotenv(_ENV_PATH)


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value):
    """递归把 ${VAR} 占位符替换为 os.environ 里的值。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def load_third_party_mcp_config() -> dict:
    """加载 mcp_servers.json，并注入环境变量。

    返回 langchain_mcp_adapters MultiServerMCPClient 所需的 server dict:
        { "<server_name>": {transport, command, args, env}, ... }
    """
    with open(_JSON_PATH, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    servers = raw.get("servers", {})
    return _expand_env(servers)


# ---- 常用配置项的便捷访问 ----
MINIO_ENDPOINT = get_env("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = get_env("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = get_env("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = get_env("MINIO_BUCKET", "uploads")
MINIO_SECURE = get_env("MINIO_SECURE", "false").lower() == "true"

WRAPPER_TMP_DIR = get_env("WRAPPER_TMP_DIR", "./tmp")

# ---- Elasticsearch（知识库向量检索存储）----
ES_URL = get_env("ES_URL", "http://localhost:9200")
ES_API_KEY = get_env("ES_API_KEY", "")
ES_USER = get_env("ES_USER", "")
ES_PASSWORD = get_env("ES_PASSWORD", "")

# ---- 向量化（TEI OpenAI 兼容端点）----
# 与 backend 读同一份值：EMBEDDING_DIMS 两边必须严格一致（ingest/query 维度对称）。
# 本地 TEI：model 任意（TEI 忽略）、check_embedding_ctx_length 必须 False（避免 tiktoken 报错）。
EMBEDDING_BASE_URL = get_env("EMBEDDING_BASE_URL", "http://localhost:8082/v1")
EMBEDDING_API_KEY = get_env("EMBEDDING_API_KEY", "-")
EMBEDDING_MODEL = get_env("EMBEDDING_MODEL", "text-embeddings-inference")
EMBEDDING_CHECK_CTX_LENGTH = get_env("EMBEDDING_CHECK_CTX_LENGTH", "false").lower() == "true"
_dims_raw = get_env("EMBEDDING_DIMS", "").strip()
EMBEDDING_DIMS = int(_dims_raw) if _dims_raw else None
EMBEDDING_TIMEOUT = int(get_env("EMBEDDING_TIMEOUT", "30"))
EMBEDDING_BATCH_SIZE = int(get_env("EMBEDDING_BATCH_SIZE", "24"))

# ---- 知识库检索默认参数 ----
KB_TOP_K = int(get_env("KB_TOP_K", "5"))

SERVER_HOST = get_env("HOST", "0.0.0.0")
SERVER_PORT = int(get_env("PORT", "8088"))
