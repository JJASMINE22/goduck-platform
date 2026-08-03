"""路由层共享工具：MinIO 客户端、字节读写、MCP 结果打平。"""
from __future__ import annotations

import io
import logging
import os
import shutil
from typing import Any

from minio import Minio

from config import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_SECRET_KEY,
    MINIO_SECURE,
    WRAPPER_TMP_DIR,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------
def build_minio_client() -> Minio:
    return Minio(
        endpoint=MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def fetch_bytes(minio_client: Minio, object_key: str) -> bytes:
    """从 MinIO 取原始字节。用 with 上下文 + resp.data（不是 read）。"""
    with minio_client.get_object(MINIO_BUCKET, object_key) as resp:
        return resp.data


def fetch_object(minio_client: Minio, object_key: str) -> tuple[bytes, str]:
    """从 MinIO 取原始字节 + 对象 Content-Type。

    Content-Type 由后端上传层 detect_mime(魔数判定)写入，是权威值；
    调用方可据此分流，无需自行判型或查 DB。
    """
    with minio_client.get_object(MINIO_BUCKET, object_key) as resp:
        content_type = resp.headers.get("Content-Type", "")
        return resp.data, content_type


# ---------------------------------------------------------------------------
# 临时文件
# ---------------------------------------------------------------------------
def ensure_tmp_dir() -> str:
    abs_dir = os.path.abspath(WRAPPER_TMP_DIR)
    os.makedirs(abs_dir, exist_ok=True)
    return abs_dir


def write_tmp_file(tmp_dir: str, object_key: str, data: bytes, suffix: str = ".bin") -> str:
    """写入临时文件，返回绝对路径。"""
    path = os.path.join(tmp_dir, f"{object_key}{suffix}")
    with open(path, "wb") as f:
        f.write(data)
    return path


def safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning("删除临时文件失败 %s: %s", path, e)


# ---------------------------------------------------------------------------
# 第三方工具调用辅助
# ---------------------------------------------------------------------------
def flatten_mcp_text(raw_result: Any) -> str:
    """将内层 MCP content blocks 打平为纯字符串。

    处理 langchain_mcp_adapters 返回的常见形态:
      - list[dict]: [{"type":"text","text":"...","id":"..."}]
      - list[str]:  ["片段1", "片段2"]
      - str:        "直接字符串"
    多个 text 块拼接，保留可读性。
    """
    if isinstance(raw_result, str):
        return raw_result
    if isinstance(raw_result, list):
        parts: list[str] = []
        for item in raw_result:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "text" in item:
                    parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(raw_result)


def get_tool(tools: dict, name: str):
    """按名取第三方工具，缺失时抛 KeyError 带可读信息。"""
    if name not in tools:
        raise KeyError(f"第三方工具 '{name}' 不在工具池中，可用: {list(tools.keys())}")
    return tools[name]
