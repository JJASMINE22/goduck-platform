"""集中配置加载（.env）。

附件解析路由（image/document）退役后，本进程仅服务知识库检索：
保留 ES / 向量化 / 服务端口配置。MinIO 与第三方 MCP 工具池配置已随
迁移移除（backend 进程直连 MinIO 与视觉模型，不经本进程中转）。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_PATH = PROJECT_ROOT / ".env"

# 启动即加载 .env
load_dotenv(_ENV_PATH)


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---- PostgreSQL（只读：kb → embedding 注册行解析，plan kb-embedding-model-registry）----
# 空 = 注册表关闭（纯 env legacy 路径）；不可达/查询失败自动回落 env（fail-open，
# 行为=历史版本），不阻断检索主链路。
DATABASE_URL = get_env("DATABASE_URL", "")

# ---- Elasticsearch（知识库向量检索存储）----
ES_URL = get_env("ES_URL", "http://localhost:9200")
ES_API_KEY = get_env("ES_API_KEY", "")
ES_USER = get_env("ES_USER", "")
ES_PASSWORD = get_env("ES_PASSWORD", "")

# ---- 向量化（TEI OpenAI 兼容端点）----
# 与 backend 读同一份值：EMBEDDING_DIMS 为「默认/兜底维度」——各库实际维度以
# ES index mapping 真值为准（kb 建库时可自定义 dims，见 get_dims 探测），
# ingest(backend 按 kb.dims) 与 query(此处按 mapping 分组) 维度对称。
# 本地 TEI：model 任意（TEI 忽略）、check_embedding_ctx_length 必须 False（避免 tiktoken 报错）。
EMBEDDING_BASE_URL = get_env("EMBEDDING_BASE_URL", "http://localhost:8082/v1")
EMBEDDING_API_KEY = get_env("EMBEDDING_API_KEY", "-")
EMBEDDING_MODEL = get_env("EMBEDDING_MODEL", "text-embeddings-inference")
EMBEDDING_CHECK_CTX_LENGTH = get_env("EMBEDDING_CHECK_CTX_LENGTH", "false").lower() == "true"
_dims_raw = get_env("EMBEDDING_DIMS", "").strip()
EMBEDDING_DIMS = int(_dims_raw) if _dims_raw else None  # 默认/兜底维度（None=模型原生）
EMBEDDING_TIMEOUT = int(get_env("EMBEDDING_TIMEOUT", "30"))
EMBEDDING_BATCH_SIZE = int(get_env("EMBEDDING_BATCH_SIZE", "24"))

# ---- 知识库检索默认参数 ----
KB_TOP_K = int(get_env("KB_TOP_K", "5"))

SERVER_HOST = get_env("HOST", "0.0.0.0")
SERVER_PORT = int(get_env("PORT", "8088"))
