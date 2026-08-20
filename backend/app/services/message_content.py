"""消息内容组装（附件校验 + KB 锚点注入）—— POST /messages 与语音 WS 共享。

从 api/stream.py 的组装段提取（纯重构，POST 行为不变）：同一套校验与组装
逻辑服务两个入口，使语音回合与手打回合发往 agent 的 content 完全一致。

两个入口：
- ``validate_message_content``：纯校验（能力门控/附件归属/KB 存在性），语音
  start 帧在录音**前**调用，让配置错误尽早暴露；
- ``build_message_content``：校验 + 组装（含附件多模态与 KB 锚点注入），
  POST 与语音终稿后各自调用（内部重复执行校验，幂等且廉价）。

错误语义：校验失败抛 ``MessageContentError``（携带与原 POST 一致的
status_code/detail）——HTTP 路由映射 HTTPException，WS 路由映射 error 帧
（回合作废但连接保持）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.core.schema_utils import AgentConfig
from app.models import db as dbmod
from app.models.schemas import Attachment

if TYPE_CHECKING:
    from app.deps import AppState


class MessageContentError(Exception):
    """校验/组装失败（能力门控 422 / 附件归属 403 / KB 不存在 404 / KB 未启用 503）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class BuiltMessage:
    """发往 agent 的组装结果：content（纯文本或多模态）+ 附件 id 列表。"""

    content: str | list
    upload_ids: list[str]


async def _load_gate_context(
    st: "AppState",
    session: dict,
    has_attachments: bool,
    has_kb_ids: bool,
) -> tuple[Optional[dict], Optional[AgentConfig]]:
    """加载能力门控所需上下文（agent 配置仅在携带附件/KB 时才查）。"""
    needs_agent_cfg = has_attachments or has_kb_ids
    agent_row = await dbmod.get_agent(st.pool, session["agent_id"]) if needs_agent_cfg else None
    agent_cfg = (
        AgentConfig.model_validate(agent_row["config_json"]) if agent_row is not None else None
    )
    return agent_row, agent_cfg


async def _validate_attachments(
    st: "AppState",
    session: dict,
    attachments: list[Attachment],
    agent_cfg: Optional[AgentConfig],
) -> list[str]:
    """① 附件：能力门控 → upload_id 有效性/归属校验。返回有序 upload_id 列表。"""
    if not attachments:
        return []
    assert agent_cfg is not None  # 携带附件时 agent 配置必已加载
    if not agent_cfg.allow_attachments:
        raise MessageContentError(422, "该智能体未启用附件能力")
    upload_ids: list[str] = []
    for a in attachments:
        upload = await dbmod.get_upload(st.pool, uuid.UUID(a.upload_id))
        if upload is None or upload["session_id"] != session["id"]:
            raise MessageContentError(403, f"无效附件: {a.upload_id}")
        upload_ids.append(a.upload_id)
    return upload_ids


async def _validate_kbs(
    st: "AppState",
    kb_ids: list[uuid.UUID],
    agent_cfg: Optional[AgentConfig],
) -> list[dict]:
    """② 知识库：能力门控 + 选中 KB 存在性。返回 KB 行（锚点注入用）。"""
    if not kb_ids:
        return []
    assert agent_cfg is not None  # 携带 KB 时 agent 配置必已加载
    if st.knowledge_service is None:
        raise MessageContentError(503, "知识库能力未启用")
    if not agent_cfg.allow_knowledge:
        raise MessageContentError(422, "该智能体未启用知识库能力")
    kbs = await dbmod.get_kbs_by_ids(st.pool, kb_ids)
    if len(kbs) != len(kb_ids):
        raise MessageContentError(404, "部分所选知识库不存在")
    return kbs


async def validate_message_content(
    st: "AppState",
    session: dict,
    attachments: Optional[list[Attachment]] = None,
    knowledge_base_ids: Optional[list[uuid.UUID]] = None,
) -> None:
    """纯校验（无组装）：录音前暴露配置/归属错误（语音 start 帧路径）。"""
    attachments = attachments or []
    knowledge_base_ids = knowledge_base_ids or []
    _, agent_cfg = await _load_gate_context(
        st, session, bool(attachments), bool(knowledge_base_ids)
    )
    await _validate_attachments(st, session, attachments, agent_cfg)
    await _validate_kbs(st, knowledge_base_ids, agent_cfg)


async def build_message_content(
    st: "AppState",
    session: dict,
    user_query: str,
    attachments: Optional[list[Attachment]] = None,
    knowledge_base_ids: Optional[list[uuid.UUID]] = None,
) -> BuiltMessage:
    """校验并组装 HumanMessage.content（plan §6.5 附件/KB 两段）。

    - ① 附件：能力门控 → upload_id 有效性/归属 → supports_vision → 多模态组装；
    - ② 知识库：能力门控 + KB 存在性 → 锚点注入（不在此检索，工具发现交给
      agent 原生 function-calling：无检索工具则按锚点原则自主降级）。
    """
    attachments = attachments or []
    knowledge_base_ids = knowledge_base_ids or []
    agent_row, agent_cfg = await _load_gate_context(
        st, session, bool(attachments), bool(knowledge_base_ids)
    )
    upload_ids = await _validate_attachments(st, session, attachments, agent_cfg)
    kbs = await _validate_kbs(st, knowledge_base_ids, agent_cfg)

    content: str | list = user_query
    if upload_ids:
        supports_vision = st.model_store.supports_vision(agent_row["model_id"])
        content = await st.content_composer.build(
            user_query=user_query,
            upload_ids=upload_ids,
            supports_vision=supports_vision,
        )
    if kbs:
        content = st.content_composer.with_kb_anchor(content, kbs)
    return BuiltMessage(content=content, upload_ids=upload_ids)
