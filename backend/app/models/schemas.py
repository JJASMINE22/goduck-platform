"""Pydantic API request / response schemas (boundary validation).

Service layer does NOT repeat type checks done here.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentCreate(BaseModel):
    name: str = Field(min_length=1)
    description: Optional[str] = None
    config: dict[str, Any] = Field(..., description="Full AgentConfig tree")
    model_id: uuid.UUID = Field(..., description="引用已注册模型 id（model 表主键，必填）")
    creator: str = Field(min_length=1)


class AgentUpdate(BaseModel):
    config: dict[str, Any]
    model_id: uuid.UUID = Field(..., description="引用已注册模型 id（可更换模型）")
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
    agent_id: uuid.UUID
    title: Optional[str] = None
    thread_id: Optional[str] = Field(
        default=None, description="Custom thread_id; auto-generated if omitted"
    )


class SessionOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    user_id: str
    thread_id: str
    title: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime
    agent_name: Optional[str] = None


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    model_id: Optional[uuid.UUID] = None
    status: str
    creator: str
    updater: Optional[str]
    created_at: datetime
    updated_at: datetime


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


# ---------------------------------------------------------------------------
# Streaming / HITL
# ---------------------------------------------------------------------------
class Attachment(BaseModel):
    upload_id: str = Field(
        ..., description="上传接口返回的 id；kind/mime 后端自查，前端不传"
    )


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)
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


class WithdrawRequest(BaseModel):
    message_id: str = Field(
        ..., min_length=1, description="LangGraph 用户消息 id，定位被回撤的整段回合"
    )


class WithdrawResponse(BaseModel):
    """回撤结果：被回撤用户消息的原文 + 附件（回填发送框）+ 实际移除的消息 id 列表。"""

    raw_content: str
    attachments: list["UploadOut"] = Field(default_factory=list)
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
    enable_thinking: bool = Field(default=False, description="思考模式开关（仅思考模型有效）")
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
    enable_thinking: bool = False
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
    enable_thinking: bool
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


class KnowledgeBaseUpdate(BaseModel):
    name: str = Field(min_length=1)
    description: Optional[str] = None


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
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class KbSearchResult(BaseModel):
    content: str
    score: float
    filename: str
    doc_id: uuid.UUID
    page: Optional[int] = None
    chunk_index: Optional[int] = None
    kb_id: Optional[uuid.UUID] = None


# MessageOut references UploadOut (defined later in this module). Pydantic v2
# rebuilds incomplete models automatically, but we resolve the forward ref
# explicitly at import end so the schema is guaranteed complete regardless of
# definition order / future refactors. Idempotent.
MessageOut.model_rebuild()
WithdrawResponse.model_rebuild()
# WorkspaceNode references itself recursively.
WorkspaceNode.model_rebuild()
