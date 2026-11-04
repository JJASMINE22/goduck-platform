"""Centralized configuration for goduck-platform.

Replaces the old `base_confs` global-singleton pattern. All paths / connection
strings / model names flow through here (pydantic-settings), nothing hardcoded.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, field_validator
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

    # ---- 登录认证（用户体系）----
    # 引导语义 create-if-absent：已存在用户以库内为准（env 密码永不覆盖应用内
    # 修改），改密/改昵称走平台内流程。生产至少引导一个 manager。
    bootstrap_users: str = Field(
        default="",
        description=(
            "启动引导用户列表：'用户名:昵称:角色:密码' 逗号分隔多条，角色 manager|user。"
            "密码仅首次建户生效（哈希入库）；单条非法仅告警跳过。"
        ),
    )
    legacy_user_map: str = Field(
        default="",
        description=(
            "存量库 VARCHAR user_id → app_user 用户名 的对齐映射：'旧值:用户名' 逗号分隔。"
            "仅在 user_id 列仍为 VARCHAR 的旧库触发（幂等，新装自动跳过）；"
            "存在未映射旧值时启动显式失败。"
        ),
    )
    auth_session_ttl_hours: int = Field(
        default=168,
        gt=0,
        description="登录会话有效期（小时）。改密/停用/管理员重置即时吊销全部会话。",
    )

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
    # 预签名 URL 相对化开关（生产 compose：nginx /minio/ 同源代理存在时置 true，
    # doc_url 变 /minio/<bucket>/<key>?sig——不外泄内网地址、ddnsto/LAN 通吃）。
    # 默认 False = SDK 原生绝对 URL（本地 dev 直连 MinIO 的既有行为，无代理依赖）。
    minio_relative_presign: bool = False

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

    # ---- 向量化（TEI 经 OpenAI 兼容端点）----
    # 注册化主路径：embedding 表（/embeddings 注册，per-KB 绑定 embedding_id）。
    # 本组 env 为启动 seed 默认行的取值源 + 迁移期回落（kb 未绑定 / mcp-servers
    # 未切换）——seed 行值与 env 同源 ⇒ 双路径行为等价（plan: kb-embedding-model-registry）。
    # 本地 TEI：部署时已指定模型 ⇒ 此处 model 任意（TEI 忽略）、无需 key（dummy 即可），
    #          只要 base_url 指向 /v1 正确即可（见 demo/agents/embeddings.py）。
    # check_embedding_ctx_length 必须为 False：否则 langchain 用 tiktoken 按 model 分词，
    #   对 TEI 部署的模型会报错。改用真实 OpenAI 云端嵌入时设 True。
    # dimensions：Qwen3-Embedding 支持 MRL，原生 1024 可经 dimensions 参数截断到 512。
    embedding_base_url: str = "http://192.168.x.x:8082/v1"  # TEI OpenAI 兼容端点（/v1）
    embedding_api_key: str = "-"                            # dummy key，TEI 不验证；云端用真实 key
    embedding_model: str = "text-embeddings-inference"      # 任意字符串，TEI 忽略；云端用真实模型名
    embedding_check_ctx_length: bool = False                # TEI 必须 False（避免 tiktoken 报错）
    embedding_dims: Optional[int] = None                    # 全局维度（MRL 截断值，如 512）；seed 与 legacy 回落共用
    # seed 默认行名（启动 create-if-absent；各环境端点经 env 自适配，名称保持统一）
    embedding_seed_name: str = "Qwen3-Embedding-0.6B（TEI 内置）"
    # 单批嵌入请求超时（秒）：必须 > 单批实际处理耗时，否则每批必超时（OpenAI 客户端超时后还会重试 2 次放大延迟）。
    embedding_timeout: int = 90
    # 单次 /v1/embeddings HTTP 请求携带的最大文本数（LangChain 客户端按此自动分批：>此值内部自动切片，
    #   不抛异常；add_documents 无需外层手动循环）。受 TEI --max-client-batch-size（默认32）上限约束（超则 422）。
    # 超时关键：须对齐 TEI --max-batch-size（默认4）。客户端 batch > 它时，单请求内 TEI 做多轮前向推理
    #   （如 batch=24 / max-batch-size=4 → 6 轮），请求挂起时间随比值放大，并发下（ingestion_concurrency>1）
    #   CPU 串行再×N → 易超时。对齐后每请求恰好 1 轮推理，延迟最低。
    embedding_batch_size: int = 4

    # ---- 知识库 / RAG ----
    kb_enabled: bool = True
    kb_store_minio: bool = True                  # False=上传即解析、不留底、不支持 reindex
    kb_top_k: int = 5
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    kb_ingestion_concurrency: int = 2
    # ---- 解析引擎开关（plan: kb-docling-parse-display C1）----
    # legacy=轻量基线（pymupdf 文本层 + 滑窗切块；**无 OCR**——扫描件解析为
    # docling 专属，扫描件在 legacy 下报"无可索引文本"并指引切换引擎）；
    # docling=结构单元直入 ES（splitter 退役，仅预算护栏）。默认 legacy：
    # 灰度切换，存量库与存量 chunk 不受影响（新字段对 legacy 单元可空）。
    kb_parser_engine: Literal["legacy", "docling"] = "legacy"
    # 长文档分片硬约束（片 = 独立 docling convert + 独立 IR 边车；整册大文档
    # convert 不可行）。默认 100，Phase A 单页耗时数据校准。
    kb_docling_shard_pages: int = Field(default=100, ge=10, le=1000)
    # CPU 并行度旋钮（AcceleratorOptions.num_threads；convert 本身进程内串行，
    # 不做多实例并行 convert——CPU 下互抢核）。GPU 设备下 torch 算子不吃该值，
    # 设备回落 cpu 时仍生效。
    kb_docling_threads: int = Field(default=4, ge=1, le=64)
    # 推理设备（AcceleratorOptions.device）：auto=按 torch 探测 cuda→mps→cpu
    # 自动选择（无 GPU 安全回落 cpu）；cpu/cuda/mps 显式锁定。注意 auto 使
    # 解析行为依赖部署环境（GPU/OCR provider 差异带来输出细微不同），该值
    # 已纳入分片 IR 边车校验（变更即缓存失效重跑解析）。
    kb_docling_device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    # 生产禁运行时下载模型：预置权重目录（空=开发期由 docling 自动下载）。
    kb_docling_artifacts_path: str = ""
    # 结构单元预算上界（超预算在子区域边界再切；与 legacy kb_chunk_size 分开
    # 命名防双轨语义串线。无 overlap 不是"上下文变多"而是"盲切风险消失"，
    # 放大目的=保护结构单元完整性。默认 2000，Phase A 单元长度分布 p95 校准；
    # 硬上限核查：预算不得越过 TEI 端点实际 max sequence length，否则静默
    # 截断制造新的边界盲切——调大前先核对 TEI 部署参数）。
    kb_unit_max_chars: int = Field(default=2000, ge=200, le=32000)

    # ---- Jev Shell 守卫（allow_shell HITL 减负；plan: jev-shell-guard）----
    # 判决只决定"是否跳过人工"，HITL 保持 fail-closed 基底：Jev 不可用/超时/解析
    # 失败一律回落"每条问人"（= 现状行为），绝无 fail-open 分支。
    jev_enabled: bool = Field(
        default=False,
        description="Jev 守卫总开关：false 时构建回落 interrupt_on={'execute': True}（现状），"
        "一切注入逻辑不生效。",
    )
    typesafe_api_key: str = Field(
        default="",
        description="TypeSafe AI API key（jev_enabled=true 时必填；缺失→不启用+启动警告，不崩）。",
    )
    jev_base_url: str = Field(
        default="https://api.typesafe.ai",
        description="TypeSafe REST 端点根（POST {base}/v1/systemone）。",
    )
    jev_model: str = Field(
        default="jev-1.13.0",
        description="Jev 模型版本。锁具体版本号、不用 jev-latest 别名（防静默行为漂移）。",
    )
    jev_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="同步调用硬超时（秒）。when/description 在 after_model 内同步执行，"
        "超时即 fail-closed 问人。",
    )
    jev_mode: str = Field(
        default="shadow",
        description="shadow=只记录标注、行为与现状一致（灰度观察）；enforce=灰区放行生效。"
        "必须用 shadow 期分歧样本重标定 jev_safe_threshold 后才可切 enforce。",
    )
    jev_safe_threshold: float = Field(
        default=0.6,
        ge=0.5,
        le=1.0,
        description="自动放行阈值 T：概率落在安全侧且该题置信度达标的票数全过才放行。"
        "占位值，切 enforce 前须重标定。",
    )
    jev_state_max_chars: int = Field(
        default=4000,
        ge=200,
        description="state 渲染截断上限（字符，含近期命令历史；API 上限 64k）。",
    )

    # ---- Jev Rubric 门控（grader 分诊；plan: jev-rubric-grader-gate）----
    # 两态 grader(llm|jev) 的平台侧总控：jev 不可用时节点配置 grader=jev 也
    # 回落 llm 并打构建期 warning（fail-visible）。阈值配置即契约——无 shadow
    # 态，首个生产值建议保守起步，靠审计（灰区轮 JEV+LLM 双判决）后验调优。
    jev_rubric_enabled: bool = Field(
        default=False,
        description="rubric 门控总开关：false 时 jev_rubric_factory 不注入，"
        "grader='jev' 的节点构建回落 llm 路径（一键回退保险丝，不动 agent 配置）。",
    )
    jev_rubric_pass_threshold: float = Field(
        default=0.9,
        ge=0.5,
        lt=1.0,
        description="rubric 快道默认阈值 T（P 与 conf 双轴同值）。三级解析："
        "行级 criterion.threshold ← 节点级 pass_threshold ← 本值。保守值=快道"
        "收窄、灰区为主（LLM 兜底），观察审计分歧率后再下调。",
    )
    jev_rubric_mode: str = Field(
        default="enforce",
        description=(
            "rubric 门控运行态：enforce=满足侧快道生效（每次评分前分诊，含首评 iter=0；"
            "全准则 P/conf 达标即合成 satisfied 跳过 LLM grader）；shadow=只判只落审计、"
            "恒委派 LLM（校准/分歧率统计用，行为与纯 llm 一致）。误放行风险由 per-agent "
            "grader=jev 自愿承担；shadow 为全局回退保险丝（不动 agent 配置）。"
        ),
    )

    # ---- Jev KB 检索门控（Agentic RAG 相关性过滤；plan: jev-kb-rerank-middleware）----
    # 形态 F2：middleware 包裹 kb_search 工具结果，写入消息史前做"高置信无关
    # 才滤"的保守过滤。fail-closed = passthrough = 现状行为（Jev 不可用/返回
    # 格式漂移/门控异常一律原样透传，绝不杀死工具结果）。平台级开关，作用于
    # 所有 allow_knowledge 主代理。
    jev_kb_rerank_enabled: bool = Field(
        default=False,
        description="kb 门控总开关：false 时 jev_kb_factory 不注入（构建不挂载，"
        "行为=现状）。前置：jev_enabled ∧ TYPESAFE_API_KEY 就绪。",
    )
    jev_kb_mode: str = Field(
        default="shadow",
        description=(
            "kb 门控运行态：shadow=只判只落审计（校准态，不改写 content）；"
            "annotate=每 chunk 加 [相关性] 前缀标注不删除；enforce=高置信无关才滤"
            "（keep_floor 保底）。灰度顺序 shadow→annotate→enforce，假阴率达标后才可收紧。"
        ),
    )
    jev_kb_drop_probability: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="drop 侧概率阈值：rel/info 两题 P 均 ≤ 此值（确定无关侧）才允许 drop。",
    )
    jev_kb_drop_confidence: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="drop 侧置信度阈值：conf ≥ 此值才允许 drop。与 shell 守卫锁 pass 侧"
        "（0.9 放行）镜像对称——不可逆动作（删内容）放高置信门后。",
    )
    jev_kb_keep_floor: int = Field(
        default=2,
        ge=1,
        description="ES 序前 N 条永不滤（Jev 只修剪尾部不斩首；输出集合永不为空的保底）。",
    )
    jev_kb_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        description="kb 门控独立 Jev 调用预算（秒；本地部署 RTT 小，与 shell 守卫预算分离）。",
    )
    jev_kb_k_boost: int = Field(
        default=0,
        ge=0,
        description="两段式检索（默认关）：>0 时 pre-wrap 改写 args.k 过采、post-wrap 筛回"
        "——recall 净增；Jev 不可用时按 ES 序截回原始 k（故障形态=现状）。",
    )

    # ---- AgentRegistry cache tuning ----
    registry_max_entries: int = 16
    registry_ttl_seconds: int = 3600

    # ---- RunManager（SSE 回合与连接解耦：run 服务端续跑 + 观察流重挂）----
    run_buffer_max_events: int = Field(
        default=2000,
        gt=0,
        description=(
            "每 run 环形事件缓冲上限（重挂回放窗口；token 事件占大头）。"
            "超出淘汰最老并推进 buffer_start，落后太多的观察者收 resync。"
        ),
    )
    run_ttl_seconds: float = Field(
        default=120,
        gt=0,
        description="run 终态后 RunState 保留窗口（晚归观察者补领终态事件），超时由 sweeper 回收。",
    )
    run_max_concurrent: int = Field(
        default=16,
        gt=0,
        description="进程内并发 run 上限（超出 503 拒绝启动，防资源失控）。",
    )
    run_subscriber_queue_max: int = Field(
        default=500,
        gt=0,
        description=(
            "单订阅者事件队列上限；满即踢出（该订阅者收 resync 后关闭），"
            "run 永不被慢消费者阻塞。"
        ),
    )
    run_sweep_interval_seconds: float = Field(
        default=15,
        gt=0,
        description="终态 TTL 回收循环的扫描间隔（秒）。",
    )

    # ---- 定时任务（cron jobs）----
    cron_enabled: bool = Field(
        default=True,
        description="定时任务总开关：false 时不启动调度循环（cron 路由仍可管理任务，仅不触发）。",
    )
    cron_tick_seconds: int = Field(
        default=30,
        ge=5,
        description="调度循环 tick 间隔（秒）。触发精度上限；DB 负载每 tick 一条索引查询，可忽略。",
    )
    cron_max_concurrency: int = Field(
        default=3,
        ge=1,
        description="后台任务并发执行上限（信号量）。1=串行，防多个 LLM 长跑互相拖垮。",
    )
    cron_run_timeout_s: float = Field(
        default=1800.0,
        gt=0,
        description="单次任务运行超时秒数；超时中止并计一次失败（30 分钟适配深度研究类长跑）。",
    )
    cron_truncation_retry_attempts: int = Field(
        default=2,
        ge=0,
        description=(
            "模型响应流截断（终末答复缺结束帧，供应商中途静默掐断连接）时的整轮自动"
            "重试次数，0=不重试。仅对截断类失败生效：重试前重置线程重新取数（cron "
            "轮次本就相互独立、热榜类工具只读幂等）。重试耗时计入 cron_run_timeout_s 总预算。"
        ),
    )
    cron_truncation_retry_backoff_s: float = Field(
        default=45.0,
        gt=0,
        description="截断重试的线性退避基数（秒）：第 n 次重试前等待 n×基数，避开供应商瞬时限流窗口。",
    )
    cron_max_consecutive_failures: int = Field(
        default=5,
        ge=1,
        description="连续失败熔断阈值：达到后自动停用任务（enabled=false），需人工重新启用。",
    )
    cron_min_interval_minutes: int = Field(
        default=5,
        ge=1,
        description="最小触发间隔（分钟）。防高频任务打爆 LLM 配额。",
    )
    cron_max_interval_minutes: int = Field(
        default=525600,
        ge=1,
        description="最大触发间隔（分钟，默认一年上限）。",
    )

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
    @field_validator("jev_mode")
    @classmethod
    def _validate_jev_mode(cls, v: str) -> str:
        """枚举收口（fail-fast）：拼错 mode 名不得静默按 shadow/enforce 之外的语义运行。"""
        allowed = {"shadow", "enforce"}
        if v not in allowed:
            raise ValueError(f"jev_mode 须为 {'/'.join(sorted(allowed))}（当前 {v!r}）")
        return v

    @field_validator("jev_rubric_mode")
    @classmethod
    def _validate_jev_rubric_mode(cls, v: str) -> str:
        """枚举收口（fail-fast），语义同 _validate_jev_mode。"""
        allowed = {"shadow", "enforce"}
        if v not in allowed:
            raise ValueError(f"jev_rubric_mode 须为 {'/'.join(sorted(allowed))}（当前 {v!r}）")
        return v

    @field_validator("jev_kb_mode")
    @classmethod
    def _validate_jev_kb_mode(cls, v: str) -> str:
        """枚举收口（fail-fast），三态：shadow/annotate/enforce（语义同 _validate_jev_mode）。"""
        allowed = {"shadow", "annotate", "enforce"}
        if v not in allowed:
            raise ValueError(f"jev_kb_mode 须为 {'/'.join(sorted(allowed))}（当前 {v!r}）")
        return v

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
