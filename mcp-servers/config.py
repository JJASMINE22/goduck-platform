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

SERVER_HOST = get_env("HOST", "0.0.0.0")
SERVER_PORT = int(get_env("PORT", "8088"))
