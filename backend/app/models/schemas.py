"""Pydantic API request / response schemas (boundary validation).

Service layer does NOT repeat type checks done here.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentCreate(BaseModel):
    name: str = Field(min_length=1)
    description: Optional[str] = None
    config: dict[str, Any] = Field(..., description="Full AgentConfig tree")
    model_id: uuid.UUID = Field(..., description="引用已注册模型 id（model 表主键，必填）")
    vision_model_id: Optional[uuid.UUID] = Field(
        default=None,
        description="下挂视觉模型 id（基座无视觉时用于图像预消费描述）。"
        "主代理须开启 allow_attachments 且基座 supports_vision=false 才允许设置"
        "（违反任一保存时 400）。",
    )
    kind: Literal["chat", "cron"] = Field(
        default="chat",
        description=(
            "用途类型：'chat'=聊天会话 | 'cron'=定时任务专用。"
            "创建后不可变（方案 A）——转换走导出 YAML 重建。"
            "决定可绑定面：chat 型不可建定时任务，cron 型不可建聊天会话。"
        ),
    )
    creator: str = Field(min_length=1)


class AgentUpdate(BaseModel):
    config: dict[str, Any]
    model_id: uuid.UUID = Field(..., description="引用已注册模型 id（可更换模型）")
    vision_model_id: Optional[uuid.UUID] = Field(
        default=None,
        description="下挂视觉模型 id；null=解除下挂。主代理须开启 allow_attachments "
        "且基座自带视觉时禁止设置（违反任一保存时 400）",
    )
    name: Optional[str] = Field(
        default=None,
        min_length=1,
        description="可选改名；省略或为 null 表示保持原名（改名时后端排除自身做唯一性校验）",
    )
    description: Optional[str] = None
    updater: str = Field(min_length=1)


class ValidateRequest(BaseModel):
    config: dict[str, Any]


class RestoreRequest(BaseModel):
    history_id: uuid.UUID
    operator: str = Field(min_length=1)


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    model_id: Optional[uuid.UUID] = None
    # 绑定模型名称（LEFT JOIN model 表；create/update 的 RETURNING 无法跨表，为 None）
    model_name: Optional[str] = None
    vision_model_id: Optional[uuid.UUID] = None
    vision_model_name: Optional[str] = None
    kind: str = Field(
        default="chat",
        description="用途类型 'chat'|'cron'（创建后不可变）；前端据此过滤建任务下拉 + 类型徽标",
    )
    status: str
    creator: str
    updater: Optional[str]
    created_at: datetime
    updated_at: datetime


class AgentDetail(AgentOut):
    config_json: dict[str, Any]
    active_mermaid: Optional[str] = None


class AgentHistoryItem(BaseModel):
    id: uuid.UUID
    action: str
    operator: str
    created_at: datetime


class ValidateResponse(BaseModel):
    valid: bool
    errors: Optional[list[str]] = None
    mermaid: Optional[str] = None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class SessionCreate(BaseModel):
    user_id: str = Field(min_length=1)
    # 可选（会话-智能体解耦）：不选则创建"空会话"，首条消息发送时才确定智能体。
    # 传入时校验存在 + 非 cron 型（service 层）。
    agent_id: Optional[uuid.UUID] = Field(
        default=None,
        description="创建时预选的智能体；缺省=空会话，进入后再选择",
    )
    title: Optional[str] = Field(
        default=None,
        description="可选标题；空白（含纯空格）归一为 None=未命名，未命名会话可多个",
    )
    thread_id: Optional[str] = Field(
        default=None, description="Custom thread_id; auto-generated if omitted"
    )

    @field_validator("title")
    @classmethod
    def _blank_title_to_none(cls, v: Optional[str]) -> Optional[str]:
        """空白标题 → None：未命名会话不参与 (user_id, title) 去重，允许多个。"""
        if isinstance(v, str):
            return v.strip() or None
        return v


class SessionOut(BaseModel):
    id: uuid.UUID
    # 最近使用的智能体（会话-智能体解耦）：send 时写入；NULL=尚未选择（空会话）
    # 或上次智能体已被删除（FK SET NULL）。前端据此初始化 composer 默认选中。
    last_agent_id: Optional[uuid.UUID] = None
    user_id: str
    thread_id: str
    title: Optional[str]
    status: str
    kind: str = Field(
        default="chat",
        description="会话类型：'chat'=普通会话 | 'cron'=定时任务专用会话（只读，消息/回撤 409）",
    )
    created_at: datetime
    updated_at: datetime
    agent_name: Optional[str] = None
    # 会话级 token 用量累计（db.add_session_usage 维护的固定五键；NULL=尚无消耗）
    usage_totals: Optional[dict] = None


class KbRef(BaseModel):
    """知识库最小引用（id + 名称）——历史/回撤回填时渲染 KB 徽标所需的全部信息。"""

    id: uuid.UUID
    name: str


class MessageOut(BaseModel):
    id: Optional[str] = None
    role: str
    name: Optional[str] = None
    content: str
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None
    # 附件元数据（仅 human 消息）：upload_ids 记录在消息 additional_kwargs（发送时写入），
    # 历史接口据此批量查 upload 表回填。前端据此渲染缩略，与 content 解耦。
    attachments: list["UploadOut"] = Field(default_factory=list)
    # 本条消息启用的知识库（仅 human 消息）：kb_ids 同样记录在 additional_kwargs，
    # 历史接口据此批量查 knowledge_base 表回填 id+名称。前端据此在用户气泡上
    # 渲染 KB 徽标——与 content（已注入锚点脚手架）解耦。
    kbs: list["KbRef"] = Field(default_factory=list)
    # 思考内容（仅 ai 消息）：additional_kwargs.reasoning_content（thinking_patch
    # 提取，随 checkpoint 持久化）。前端历史回放据此重建"思考过程"折叠块，
    # 与实时流 ThinkingBlock 同构；无思考内容的消息该字段为 null（同 name 等既有可空字段）。
    reasoning: Optional[str] = None


# ---------------------------------------------------------------------------
# Streaming / HITL
# ---------------------------------------------------------------------------
class Attachment(BaseModel):
    upload_id: str = Field(
        ..., description="上传接口返回的 id；kind/mime 后端自查，前端不传"
    )


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)
    # 本回合使用的智能体（会话-智能体解耦，写者语义）：缺省回落会话
    # last_agent_id；两者皆无 → 409。发送成功后写入 last_agent_id。
    agent_id: Optional[uuid.UUID] = Field(
        default=None,
        description="本回合智能体；缺省回落会话最近使用的智能体",
    )
    rubric: Optional[str] = Field(
        default=None, description="Per-invoke rubric override (FixedRubric priority)"
    )
    attachments: list[Attachment] = Field(default_factory=list)
    knowledge_base_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="本次消息启用的知识库 id；非空时后端注入 KB 锚点（强暗示 agent 调 kb_search）。",
    )


class DecisionEditAction(BaseModel):
    name: str
    args: dict[str, Any]


class Decision(BaseModel):
    type: Literal["approve", "edit", "reject", "respond"]
    message: Optional[str] = None
    edited_action: Optional[DecisionEditAction] = None


class ResumeRequest(BaseModel):
    decisions: list[Decision]


# ---------------------------------------------------------------------------
# 语音输入（WS /sessions/{id}/voice 上行控制帧）
# ---------------------------------------------------------------------------
class VoiceStartFrame(BaseModel):
    """按下麦克风时发送的回合起始帧：草稿/附件/KB/rubric 快照，校验错误在录音前暴露。

    字段语义与 SendMessageRequest 对齐（能力门控/归属校验走同一共享组装器），
    使语音回合与手打回合的消息组装行为完全一致；draft 为按压时输入框草稿
    快照，终稿非空时与之合并为一条消息发送（空转写不吞草稿）。
    """

    type: Literal["start"]
    draft: Optional[str] = Field(
        default=None, description="按压时输入框草稿快照；与语音终稿合并为一条消息"
    )
    # 写者语义（与 SendMessageRequest.agent_id 对齐）：本回合智能体，缺省回落
    # 会话 last_agent_id；语音发送成功同样写入 last_agent_id。
    agent_id: Optional[uuid.UUID] = Field(
        default=None, description="本回合智能体；缺省回落会话最近使用的智能体"
    )
    attachments: list[Attachment] = Field(default_factory=list)
    knowledge_base_ids: list[uuid.UUID] = Field(default_factory=list)
    rubric: Optional[str] = None


class WithdrawRequest(BaseModel):
    message_id: str = Field(
        ..., min_length=1, description="LangGraph 用户消息 id，定位被回撤的整段回合"
    )
    # 弱绑定解析（plan §4.4）：回撤只需任一 same-schema 图，显式传入时优先于
    # last_agent_id（last 悬空时"仅选中、不发送"即可恢复回撤）；缺省回落。
    agent_id: Optional[uuid.UUID] = Field(
        default=None,
        description="回撤取图所用智能体；缺省回落会话最近使用的智能体",
    )


class WithdrawResponse(BaseModel):
    """回撤结果：被回撤用户消息的原文 + 附件 + 知识库选择（回填发送框）+ 实际移除的消息 id 列表。"""

    raw_content: str
    attachments: list["UploadOut"] = Field(default_factory=list)
    kbs: list["KbRef"] = Field(default_factory=list)
    removed_ids: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ToolInfo(BaseModel):
    name: str
    description: str


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
class McpServerCreate(BaseModel):
    name: str = Field(min_length=1)
    kind: Literal["stdio", "http", "sse", "streamable_http"]
    config: dict[str, Any] = Field(..., description="{command,args,env} | {url,headers}")
    enabled: bool = True


class McpServerOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    config_json: dict[str, Any]
    enabled: bool
    created_at: datetime


class McpServerStatus(BaseModel):
    """单个 MCP server 的连接状态（health_check 聚合结构的成员）。"""

    name: str
    healthy: bool
    tool_count: int
    kind: str
    error: Optional[str] = None


class McpStatusResponse(BaseModel):
    """GET /mcp/status 返回：顶部聚合指标 + 每 server 明细。

    healthy = 无任何 server 处于 down（空集为 True）。
    """

    healthy: bool
    server_count: int
    tool_count: int
    servers: list[McpServerStatus]


class SkillInfo(BaseModel):
    name: str
    file_count: int


class SkillDetail(BaseModel):
    """GET /skills/{name}/description —— 解析 SKILL.md frontmatter 的 description。"""

    name: str
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Upload / attachment capability
# ---------------------------------------------------------------------------
class UploadOut(BaseModel):
    upload_id: uuid.UUID = Field(serialization_alias="upload_id")
    kind: str
    filename: str
    mime: str
    size: int
    page_count: Optional[int] = None
    preview: Optional[str] = None

    model_config = {"populate_by_name": True}


class AttachmentCapability(BaseModel):
    allow_attachments: bool
    allow_image: bool
    allow_file: list[str]
    max_size_mb: int


class KnowledgeCapability(BaseModel):
    """GET /sessions/{id}/knowledge-capability —— 驱动会话框 KB 选择器可见性。

    allow_knowledge = 智能体配置允许 AND 全局 KB 能力已启用（kb_enabled）。
    """

    allow_knowledge: bool


class ShellCapability(BaseModel):
    """GET /sessions/{id}/shell-capability —— 驱动会话页 shell 能力徽标（准则 F2）。

    allow_shell = 该会话智能体（主代理或任一子代理）配置 allow_shell=true。
    True 时该智能体获得 execute 工具（Route B 本地 shell，per-user cwd，
    强制 execute HITL）。
    """

    allow_shell: bool


# ---------------------------------------------------------------------------
# Workspace browsing (per-user isolation)
# ---------------------------------------------------------------------------
class WorkspaceCapability(BaseModel):
    """GET /sessions/{id}/workspace-capability —— 驱动会话页工作区面板可见性（准则 F2）。

    allow_workspace = 该会话智能体配置 workspace=true（前端据此决定是否挂载面板）。
    """

    allow_workspace: bool


class WorkspaceNode(BaseModel):
    """工作区文件树节点（递归）。path 为相对用户工作区根的正斜杠相对路径。"""

    name: str
    path: str
    is_dir: bool
    size: int
    modified_at: Optional[str] = None
    children: Optional[list["WorkspaceNode"]] = None


class WorkspaceFileContent(BaseModel):
    """GET /sessions/{id}/workspace/file —— 文本文件内容（二进制仅返回元信息）。

    is_text=False 时 content 为 None：前端改走 download / preview（markitdown）。
    truncated=True 表示超过返回上限被截断（请下载查看完整内容）。
    """

    path: str
    name: str
    size: int
    is_text: bool
    content: Optional[str] = None
    truncated: bool = False
    modified_at: Optional[str] = None


class WorkspacePreview(BaseModel):
    """GET /sessions/{id}/workspace/preview —— office 文档经 markitdown 转出的 Markdown。"""

    path: str
    markdown: str


class WorkspaceDeleted(BaseModel):
    """DELETE /sessions/{id}/workspace —— 删除结果（用户裁剪工作区拓扑）。"""

    deleted: str
    name: str
    was_dir: bool


class Capabilities(BaseModel):
    """全局能力开关（GET /health 返回）—— 前端据此驱动菜单/路由/页面可见性
    （DESIGN 准则 F2 能力驱动 UI）。单一真相源在后端 settings，前端零配置。"""

    kb_enabled: bool
    voice_enabled: bool = False
    jev_enabled: bool = Field(
        default=False,
        description="Jev shell 守卫可用（jev_enabled 且 TYPESAFE_API_KEY 已配）。"
        "驱动智能体编辑器 shell_guard 开关可见性。",
    )
    jev_rubric_enabled: bool = Field(
        default=False,
        description="Jev rubric 门控可用（jev 基础能力 ∧ jev_rubric_enabled）。"
        "驱动智能体编辑器 rubric grader=jev 模式可见性。",
    )
    jev_kb_rerank_enabled: bool = Field(
        default=False,
        description="Jev KB 检索门控可用（jev 基础能力 ∧ jev_kb_rerank_enabled）。"
        "平台级自动行为（非 per-agent 配置面），前端仅作状态展示。",
    )


class HealthResponse(BaseModel):
    """GET /health —— 后端存活 + 全局能力。"""

    status: str
    capabilities: Capabilities


# ---------------------------------------------------------------------------
# Registered model (provider binding)
# ---------------------------------------------------------------------------
class ModelCreate(BaseModel):
    name: str = Field(min_length=1, description="唯一展示名，如 '我的OpenAI'")
    provider_kind: Literal["openai_compatible"] = Field(
        default="openai_compatible",
        description="客户端族枚举。当前仅 openai_compatible（通吃 OpenAI/DeepSeek/Qwen/本地等）",
    )
    model_type: str = Field(..., min_length=1, description="模型型号，如 'gpt-4o-mini'")
    api_key: str = Field(..., min_length=1)
    base_url: Optional[str] = Field(default=None, description="OpenAI 兼容端点；原生 OpenAI 留空")
    supports_vision: bool = Field(default=False)
    temperature: Optional[float] = Field(default=None, ge=0, le=2, description="可空=None 用默认")
    top_p: Optional[float] = Field(default=None, ge=0, le=1, description="可空=None 用默认")
    enable_thinking: Optional[bool] = Field(
        default=None,
        description="思考模式三态：null=不注入（用 provider 默认）/ true=显式开启 / "
        "false=显式关闭。注：GLM V 系列等服务端默认开启思考，关闭须显式 false。",
    )
    enabled: bool = True


class ModelUpdate(BaseModel):
    name: str = Field(min_length=1)
    provider_kind: Literal["openai_compatible"] = "openai_compatible"
    model_type: str = Field(min_length=1)
    api_key: Optional[str] = Field(
        default=None, description="留 None 表示保留旧 key；非空则覆盖（轮换密钥）"
    )
    base_url: Optional[str] = None
    supports_vision: bool = False
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    enable_thinking: Optional[bool] = None
    enabled: bool = True


class ModelOut(BaseModel):
    id: uuid.UUID
    name: str
    provider_kind: str
    model_type: str
    base_url: Optional[str] = None
    supports_vision: bool
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    enable_thinking: Optional[bool] = None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ModelTestRequest(BaseModel):
    provider_kind: Literal["openai_compatible"] = "openai_compatible"
    model_type: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Knowledge base (RAG)
# ---------------------------------------------------------------------------
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, description="唯一知识库名")
    description: Optional[str] = None
    creator: str = Field(min_length=1)
    dims: Optional[int] = Field(
        default=None, ge=2, le=4096,
        description="库级嵌入维度（ES dense_vector 上限 4096）；缺省=全局 EMBEDDING_DIMS",
    )


class KnowledgeBaseUpdate(BaseModel):
    name: str = Field(min_length=1)
    description: Optional[str] = None
    dims: Optional[int] = Field(
        default=None, ge=2, le=4096,
        description="新维度；与库当前维度不同时触发守卫（见 force）",
    )
    force: bool = Field(
        default=False,
        description="维度变更确认位：库内有文档时须 true——索引块清空、全部文档重置为"
                    "待解析（原始文件保留，需手动重新解析），不删除任何文档数据",
    )


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str] = None
    es_index_name: str
    dims: Optional[int] = None
    status: str
    doc_count: int = 0
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class KbDocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    mime: str
    size: int
    page_count: Optional[int] = None
    status: str
    chunk_count: Optional[int] = None
    progress: Optional[int] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    doc_version: Optional[int] = Field(
        default=1, description="kb-src 归档版本（当前恒 1；语义扩展点见 db 迁移注释）"
    )
    parser_engine: Optional[str] = Field(
        default=None, description="产出当前 chunk 的引擎（legacy|docling；NULL=legacy/历史行）"
    )
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class KbParseRequest(BaseModel):
    """手动解析触发参数：均缺省时用全局默认（kb_chunk_size/kb_chunk_overlap）。"""
    chunk_size: Optional[int] = Field(default=None, ge=200, le=8000)
    chunk_overlap: Optional[int] = Field(default=None, ge=0, le=2000)


class KbSearchResult(BaseModel):
    """检索命中（D1）：docling 证据锚定字段全可空——legacy 单元 / docx 直解
    单元 / 归档失败兜底同轨 None，前端"存在即锚定、缺失即文本态降级"（B2 契约）。"""

    content: str
    score: float
    filename: str
    doc_id: uuid.UUID
    page: Optional[int] = None
    chunk_index: Optional[int] = None
    kb_id: Optional[uuid.UUID] = None
    # ---- docling 结构单元扩展（对 legacy 命中恒为缺省）----
    kind: Optional[str] = Field(default=None, description="单元类型 text|table")
    heading_path: Optional[list[str]] = Field(default=None, description="标题面包屑")
    page_rotate: Optional[int] = None
    doc_uri: Optional[str] = Field(default=None, description="证据源 MinIO 键（kb-src/...）")
    doc_url: Optional[str] = Field(default=None, description="doc_uri 预签名 URL（viewer 消费）")
    bbox: Optional[dict] = Field(
        default=None, description="归一化锚框 {x0,y0,x1,y1}（左上原点 0..1）"
    )
    assets: Optional[list[dict]] = Field(
        default=None, description="绑定图片资产 [{kind,page,bbox,caption,thumb_b64}]"
    )


class KbParserEngineOut(BaseModel):
    """解析引擎状态下发（C1）：驱动前端解析面板双轨语义（docling 隐藏滑窗
    参数、改释为结构单元预算），与后端 kb_parser_engine/kb_unit_max_chars 同源。"""

    engine: str = Field(description="legacy | docling")
    unit_max_chars: int = Field(description="结构单元预算上界（仅 docling 路径生效）")


class KbAnchorCardItem(BaseModel):
    """锚定卡取回键组 = kb_search 工具结果 hits 的定位键（确定性 chunk id）。"""

    kb_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_index: int = Field(ge=0)


class KbAnchorCardsRequest(BaseModel):
    """聊天侧 kb_search 工具卡的锚定补全（批量，懒取）。"""

    items: list[KbAnchorCardItem] = Field(min_length=1, max_length=50)


class KbAnchorCard(BaseModel):
    """单命中锚定卡：docling 证据锚定字段全可空（B2 可空契约；legacy/缺失位
    前端降级为纯文本行）。thumb_b64 仅列表预览；VLM/reader 原图回源走 MinIO。"""

    content: str
    filename: str = ""
    heading_path: Optional[list[str]] = None
    page: Optional[int] = None
    page_rotate: Optional[int] = None
    kind: Optional[str] = None
    doc_uri: Optional[str] = None
    doc_url: Optional[str] = None
    bbox: Optional[dict] = None
    assets: Optional[list[dict]] = None


# ---------------------------------------------------------------------------
# Cron（定时任务）
# ---------------------------------------------------------------------------
class CronJobCreate(BaseModel):
    user_id: str = Field(min_length=1)
    agent_id: uuid.UUID = Field(..., description="绑定智能体（须为 active 状态）")
    name: str = Field(min_length=1, max_length=100, description="任务名（同用户内唯一）")
    prompt: str = Field(min_length=1, description="任务内容：每次触发注入的固定 user query")
    interval_minutes: int = Field(
        ...,
        ge=1,
        description="触发间隔（分钟）。业务下限见服务层校验（默认 ≥5，防高频烧配额）。",
    )
    start_at: Optional[datetime] = Field(
        default=None,
        description=(
            "首次运行时间（ISO 8601，建议带时区偏移；无时区按 UTC）。"
            "None=立即（下个 tick 内触发首次）；过去时间自动夹到当前时刻。"
            "interval 的相位以 start_at 为锚。"
        ),
    )

    @field_validator("name", "prompt")
    @classmethod
    def _strip_blank(cls, v: str) -> str:
        return v.strip()


class CronJobUpdate(BaseModel):
    """部分更新（exclude_unset 语义）：仅显式提供的字段生效。

    调度规则：显式 start_at=该时刻首跑；仅改 interval=一个完整新间隔后首跑；
    重新启用（enabled false→true）=一个间隔后首跑 + 复位熔断计数。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    prompt: Optional[str] = Field(default=None, min_length=1)
    interval_minutes: Optional[int] = Field(default=None, ge=1)
    start_at: Optional[datetime] = None
    enabled: Optional[bool] = None

    @field_validator("name", "prompt")
    @classmethod
    def _strip_blank(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if isinstance(v, str) else v


class CronJobOut(BaseModel):
    id: uuid.UUID
    user_id: str
    agent_id: uuid.UUID
    agent_name: Optional[str] = None
    session_id: uuid.UUID
    name: str
    prompt: str
    interval_minutes: int
    start_at: datetime
    enabled: bool
    next_run_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    created_at: datetime
    updated_at: datetime


class CronRunOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    status: str
    trigger: str
    error: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None


# MessageOut references UploadOut (defined later in this module). Pydantic v2
# rebuilds incomplete models automatically, but we resolve the forward ref
# explicitly at import end so the schema is guaranteed complete regardless of
# definition order / future refactors. Idempotent.
MessageOut.model_rebuild()
WithdrawResponse.model_rebuild()
# WorkspaceNode references itself recursively.
WorkspaceNode.model_rebuild()
