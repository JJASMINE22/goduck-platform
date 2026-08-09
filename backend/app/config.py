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
    # 相对路径锚定到后端包根（见 local_dir），故默认 "data" → backend/data/；
    # 容器内 compose 设 LOCAL_ROOT=/data（绝对）即覆盖。统一镜像与本地为 "data" 语义。
    local_root: str = "data"

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

    # ---- Elasticsearch（知识库向量/全文检索存储）----
    es_url: str = "http://localhost:9200"
    es_api_key: Optional[str] = None
    es_user: Optional[str] = None
    es_password: Optional[str] = None

    # ---- 向量化（TEI 经 OpenAI 兼容端点；单一固定后端 + 全局单一维度）----
    # 本地 TEI：部署时已指定模型 ⇒ 此处 model 任意（TEI 忽略）、无需 key（dummy 即可），
    #          只要 base_url 指向 /v1 正确即可（见 demo/agents/embeddings.py）。
    # check_embedding_ctx_length 必须为 False：否则 langchain 用 tiktoken 按 model 分词，
    #   对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
    # dimensions：全局唯一来源，backend(入库) 与 mcp-servers(检索) 各读同一值 ⇒ ingest/query 维度对称。
    #   Qwen3-Embedding 支持 MRL，原生 1024 可经 dimensions 参数截断到 512。
    embedding_base_url: str = "http://192.168.x.x:8082/v1"  # TEI OpenAI 兼容端点（/v1）
    embedding_api_key: str = "-"                            # dummy key，TEI 不验证；云端用真实 key
    embedding_model: str = "text-embeddings-inference"      # 任意字符串，TEI 忽略；云端用真实模型名
    embedding_check_ctx_length: bool = False                # TEI 必须 False（避免 tiktoken 报错）
    embedding_dims: Optional[int] = None                    # 全局维度（MRL 截断值，如 512）；ingest 与 query 共用
    embedding_timeout: int = 30
    # 单次 /v1/embeddings HTTP 请求携带的最大文本数（LangChain 客户端按此自动分批：>此值内部自动切片，
    #   不抛异常；add_documents 无需外层手动循环）。
    # 必须 ≤ TEI --max-client-batch-size（TEI 默认 32）：超则 TEI 入口校验返回 422（batch size exceeded）。
    # 注意对齐对象是 max-client-batch-size（单请求输入数），不是 max-concurrent-requests（跨请求在途序列数）。
    embedding_batch_size: int = 24

    # ---- 知识库 / RAG ----
    kb_enabled: bool = True
    kb_store_minio: bool = True                  # False=上传即解析、不留底、不支持 reindex
    kb_top_k: int = 5
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    kb_ingestion_concurrency: int = 2

    # ---- AgentRegistry cache tuning ----
    registry_max_entries: int = 16
    registry_ttl_seconds: int = 3600

    # ---- Derived path properties (resolved relative to local_root) ----
    @property
    def local_dir(self) -> Path:
        root = Path(self.local_root)
        if not root.is_absolute():
            # 相对路径锚定到后端包根（config.py 的 parents[1] == backend/），与启动 CWD 解耦。
            # 避免 LOCAL_ROOT="." 时 local_dir 随 uvicorn 启动目录漂移，导致 workspace/
            # memories/ skills/ 落到意外位置（文件出现在与预期 workspace 平级的目录）。
            root = Path(__file__).resolve().parents[1] / root
        p = root.resolve()
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
