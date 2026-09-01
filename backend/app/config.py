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
    mcp_connect_timeout: int = Field(
        default=45,
        gt=0,
        description=(
            "单个 MCP server 连接（含 stdio 子进程握手）超时秒数。"
            "无此上限时，不可达的 uvx/uv stdio server（如 pypi 被墙时每次解析重试 "
            "~44s）会把整轮 refresh 乃至保存接口拖到分钟级。"
        ),
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

    # ---- 附件解析（发送时预消费：图像视觉描述 + 文档转 Markdown）----
    attachment_content_max_kb: int = Field(
        default=24,
        gt=0,
        description="单附件注入 content 的内容截断上限（KB）；超出截断并以'已截断'标记收尾。",
    )
    attachment_describe_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description="单图视觉描述调用超时秒数；超时/异常降级为无图 content（不中断回合）。",
    )
    attachment_parse_concurrency: int = Field(
        default=3,
        gt=0,
        description="附件解析并发上限（信号量）。markitdown 为同步 CPU 密集转换，"
        "经 to_thread 占用线程池，需限流防打满。",
    )

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
    # 单批嵌入请求超时（秒）：必须 > 单批实际处理耗时，否则每批必超时（OpenAI 客户端超时后还会重试 2 次放大延迟）。
    embedding_timeout: int = 90
    # 单次 /v1/embeddings HTTP 请求携带的最大文本数（LangChain 客户端按此自动分批：>此值内部自动切片，
    #   不抛异常；add_documents 无需外层手动循环）。受 TEI --max-client-batch-size（默认32）上限约束（超则 422）。
    # 超时关键：须对齐 TEI --max-batch-size（默认4）。客户端 batch > 它时，单请求内 TEI 做多轮前向推理
    #   （如 batch=24 / max-batch-size=4 → 6 轮），请求挂起时间随比值放大，并发下（ingestion_concurrency>1）
    #   CPU 串行再×N → 易超时。对齐后每请求恰好 1 轮推理，延迟最低。
    #   800字中文实测：batch=4→6.5s / batch=24→53s（空闲）；并发×2。
    embedding_batch_size: int = 4

    # ---- 知识库 / RAG ----
    kb_enabled: bool = True
    kb_store_minio: bool = True                  # False=上传即解析、不留底、不支持 reindex
    kb_top_k: int = 5
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    kb_ingestion_concurrency: int = 2
    # 打印件 PDF（无文本层扫描件）OCR 兜底：依赖 rapidocr-onnxruntime（未安装自动
    # 降级为仅文本层提取，行为同旧版；空文档报错信息会提示安装）。
    kb_pdf_ocr_enabled: bool = True
    # OCR 渲染 DPI：200 对常规印刷扫描件足够；小字号工程手册可提到 300
    # （页面像素数×2.25，OCR 耗时同步放大）。
    kb_pdf_ocr_dpi: int = Field(default=200, ge=72, le=600)
    # 页文本层字符数低于该阈值 → 视为扫描页触发 OCR（原生文本层页零成本直通）。
    kb_pdf_ocr_min_chars: int = Field(default=16, ge=1)

    # ---- AgentRegistry cache tuning ----
    registry_max_entries: int = 16
    registry_ttl_seconds: int = 3600

    # ---- 语音输入（STT 触发会话，单 WS 全管线）----
    voice_enabled: bool = Field(
        default=False,
        description="语音能力总开关：false 时不挂载 /sessions/{id}/voice WS 路由（健康能力位同步为 false）。",
    )
    assemblyai_api_key: str = Field(
        default="",
        description="AssemblyAI Streaming STT API key（voice_enabled=true 时必填，启动时校验）。",
    )
    stt_sample_rate: int = Field(default=16000, gt=0, description="STT 采样率（PCM16 mono；前端采麦按此重采样）")
    stt_speech_model: str = Field(
        default="universal-3-5-pro",
        description=(
            "AssemblyAI Streaming 语音模型（v3 WS speech_model 连接参数，可切换）。"
            "universal-3-5-pro=旗舰（官方默认，18 语种含中文 ZH 与日语，语种间原生码切换）；"
            "universal-streaming-english=仅英语（低成本）；"
            "universal-streaming-multilingual=EN/ES/DE/FR/PT/IT（不含中文）。"
            "注意：流式 STT 无 language_code 参数（那是预录制音频专用），语言能力由模型决定——"
            "中文转录必须使用 universal-3-5-pro。"
        ),
    )
    stt_flush_timeout: float = Field(
        default=5.0,
        gt=0,
        description=(
            "松开麦克风（flush→Terminate）后等待供应商终稿的兜底秒数；"
            "超时按空转写收场（stt_empty），不让回合挂死。"
        ),
    )
    voice_heartbeat_interval: int = Field(
        default=30,
        gt=0,
        description="浏览器⇄后端常驻 WS 空闲期应用层 ping 间隔（秒）；应用层 ping/pong 可穿透所有代理。",
    )
    voice_idle_timeout: int = Field(
        default=600,
        gt=0,
        description="常驻 WS 最大空闲秒数，超时服务端主动 close(4408) 回收（客户端自动重连）。",
    )

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
