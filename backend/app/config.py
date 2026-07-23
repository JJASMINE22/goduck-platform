"""Centralized configuration for goduck-platform.

Replaces the old `base_confs` global-singleton pattern. All paths / connection
strings / model names flow through here (pydantic-settings), nothing hardcoded.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    root_path: str = Field(
        default="",
        description=(
            "ASGI root_path：反代剥离前缀后，用于正确生成 docs/openapi.json 链接。"
            "生产经 nginx（剥 /api）时设为 /api；本地直连后端留空。仅影响文档链接，不参与路由。"
        ),
    )
    log_level: str = "INFO"

    # ---- PostgreSQL ----
    database_url: str = Field(
        default="postgresql://localhost/langgraph_db",
        description="asyncpg / psycopg connection string",
    )
    db_pool_max_size: int = 10

    # ---- LLM (legacy defaults; per-agent models now live in the `model` table) ----
    max_invoke_retries: int = 2

    # ---- Local filesystem roots (absolute, config-injected) ----
    local_root: str = "."

    # ---- MCP gateway (loopback allow flag) ----
    mcp_allow_loopback: bool = Field(
        default=True,
        description="允许 http MCP server 指向回环/内网地址 (SSRF 防护开关)。",
    )

    # ---- MinIO (附件字节持久层) ----
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "uploads"
    minio_secure: bool = False

    # ---- 附件上传约束 ----
    attachment_max_size_mb: int = 10
    attachment_max_images_per_message: int = 2

    # ---- AgentRegistry cache tuning ----
    registry_max_entries: int = 16
    registry_ttl_seconds: int = 3600

    # ---- Derived path properties (resolved relative to local_root) ----
    @property
    def local_dir(self) -> Path:
        p = Path(self.local_root).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def workspace_dir(self) -> Path:
        p = self.local_dir / "workspace"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def memories_dir(self) -> Path:
        p = self.local_dir / "memories"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skills_dir(self) -> Path:
        p = self.local_dir / "skills"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def skills_pool_dir(self) -> Path:
        # alias: skills pool (personal, shared across agents)
        return self.skills_dir


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
