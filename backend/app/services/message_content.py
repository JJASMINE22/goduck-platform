"""消息内容校验 + 组装（附件预消费 + KB 锚点注入）—— POST /messages 与语音 WS 共享。

两段式（plan §3.5）：
- ``validate_message_content``：纯校验（流外）。能力门控/每消息图像上限/
  附件归属/KB 存在性——语音 start 帧在录音**前**调用，POST 在 SSE 开流前
  调用，配置错误以 HTTP 状态码（或 WS error 帧）尽早暴露；
- ``assemble_message_content_stream``：组装（流内 async generator）。逐条
  yield ``status`` 事件（正在解析/已跳过），最终 yield ``BuiltMessage``。
  解析函数保持纯 ``async -> T``，SSE/WS 编排语义只活在调用方的 event_gen
  里（本生成器只产出事件对象，不触碰传输层）。

错误语义：校验失败抛 ``MessageContentError``（status_code/detail）；
组装期单附件解析失败**永不中断回合**——降级为"仅属性行 + status(skipped)"
提示，其余附件与 query 照常进 agent。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Optional

from app.core.schema_utils import AgentConfig
from app.models import db as dbmod
from app.models.schemas import Attachment
from app.services.attachment_policy import AttachmentPolicyService
from app.services.content_composer import AttachmentBlock, ContentComposer, DocParseError
from app.services.vision_describer import DescriberError
from app.streaming.events import SSEEvent

if TYPE_CHECKING:
    from app.deps import AppState


class MessageContentError(Exception):
    """校验失败（能力门控 422 / 附件归属 403 / KB 不存在 404 / KB 未启用 503）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class BuiltMessage:
    """发往 agent 的组装结果：content（纯文本或多模态）+ 附件 id 列表。"""

    content: str | list
    upload_ids: list[str]


def _status_event(phase: str, **payload) -> SSEEvent:
    return SSEEvent(type="status", payload={"phase": phase, **payload})


async def _validate_attachments(
    st: "AppState",
    session: dict,
    attachments: list[Attachment],
    allow_attachments: bool,
    allow_image: bool,
) -> list[str]:
    """① 附件：能力门控 → 每消息图像上限 → upload_id 有效性/归属。返回有序 id。"""
    if not attachments:
        return []
    if not allow_attachments:
        raise MessageContentError(422, "该智能体未启用附件能力")

    upload_ids: list[str] = []
    image_count = 0
    for a in attachments:
        upload = await dbmod.get_upload(st.pool, uuid.UUID(a.upload_id))
        if upload is None or upload["session_id"] != session["id"]:
            raise MessageContentError(403, f"无效附件: {a.upload_id}")
        if upload["kind"] == "image":
            image_count += 1
        upload_ids.append(a.upload_id)

    max_images = st.settings.attachment_max_images_per_message
    if image_count > max_images:
        raise MessageContentError(422, f"单条消息最多携带 {max_images} 张图片")

    # 发送门纵深：上传门已挡，此处拦截 TOCTOU（上传后 agent 换模型/下挂被禁用）。
    # 显式 422 优于静默跳过——用户必须知道图像不会被看到。
    if image_count and not allow_image:
        raise MessageContentError(
            422, "该智能体当前不支持图像附件（模型配置已变更），请移除图片后重试"
        )
    return upload_ids


async def _validate_kbs(
    st: "AppState",
    kb_ids: list[uuid.UUID],
    allow_knowledge: bool,
) -> list[dict]:
    """② 知识库：能力门控 + 选中 KB 存在性。返回 KB 行（锚点注入用）。"""
    if not kb_ids:
        return []
    if st.knowledge_service is None:
        raise MessageContentError(503, "知识库能力未启用")
    if not allow_knowledge:
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
    """纯校验（无组装）：录音前/SSE 开流前暴露配置/归属错误。"""
    attachments = attachments or []
    knowledge_base_ids = knowledge_base_ids or []

    policy = await AttachmentPolicyService(st).for_session(session)
    agent_cfg: Optional[AgentConfig] = None
    if attachments or knowledge_base_ids:
        agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
        agent_cfg = (
            AgentConfig.model_validate(agent_row["config_json"]) if agent_row else None
        )

    await _validate_attachments(
        st, session, attachments,
        allow_attachments=policy.allow_attachments,
        allow_image=policy.allow_image,
    )
    await _validate_kbs(
        st, knowledge_base_ids,
        allow_knowledge=bool(agent_cfg and agent_cfg.allow_knowledge),
    )


# ----------------------------------------------------------------------
# 流内组装（SSE/WS 编排由调用方消费：SSEEvent → 事件流/帧，BuiltMessage → 终值）
# ----------------------------------------------------------------------
async def _process_one(
    composer: ContentComposer,
    upload: dict,
    *,
    base_supports_vision: bool,
    vision_model_id: Optional[uuid.UUID],
) -> AttachmentBlock:
    """单附件预消费路由（§3.3 矩阵）。任何异常 → failed 块（降级形态）。"""
    try:
        if upload["kind"] == "image":
            if base_supports_vision:
                return AttachmentBlock(upload, image_data_url=await composer.image_data_url(upload))
            if vision_model_id is None:  # 纵深：策略层已挡，防御性兜底
                return AttachmentBlock(upload, failed=True, fail_reason="无可用图像解析能力")
            desc = await composer.describe_image(upload, vision_model_id)
            return AttachmentBlock(upload, text=desc)
        # 文档：markitdown 预解析（缓存优先）+ 截断硬上限
        markdown = await composer.doc_markdown(upload)
        text, truncated = composer.truncate(markdown)
        return AttachmentBlock(
            upload, text=text, truncated=truncated,
            orig_bytes=len(markdown.encode("utf-8")),
        )
    except (DescriberError, DocParseError) as e:
        return AttachmentBlock(upload, failed=True, fail_reason=str(e))
    except Exception as e:  # noqa: BLE001 — MinIO 取字节失败等，同样降级
        return AttachmentBlock(upload, failed=True, fail_reason=str(e))


async def assemble_message_content_stream(
    st: "AppState",
    session: dict,
    user_query: str,
    attachments: Optional[list[Attachment]] = None,
    knowledge_base_ids: Optional[list[uuid.UUID]] = None,
) -> AsyncIterator[SSEEvent | BuiltMessage]:
    """校验并组装（流内）。yield SSEEvent(status) 若干次，最终 yield BuiltMessage。

    前置：调用方已完成 validate_message_content（本生成器幂等重跑校验，
    廉价且防窗口期漂移）。
    """
    attachments = attachments or []
    knowledge_base_ids = knowledge_base_ids or []

    policy = await AttachmentPolicyService(st).for_session(session)
    # 与 validate_message_content 同条件：KB 门控同样需要 agent 配置（否则
    # 仅带 KB 的消息会因 agent_cfg=None 被误判为"未启用知识库能力"）
    agent_row = (
        await dbmod.get_agent(st.pool, session["agent_id"])
        if (attachments or knowledge_base_ids)
        else None
    )
    agent_cfg = (
        AgentConfig.model_validate(agent_row["config_json"]) if agent_row else None
    )

    upload_ids = await _validate_attachments(
        st, session, attachments,
        allow_attachments=policy.allow_attachments,
        allow_image=policy.allow_image,
    )
    kbs = await _validate_kbs(
        st, knowledge_base_ids,
        allow_knowledge=bool(agent_cfg and agent_cfg.allow_knowledge),
    )

    content: str | list = user_query
    if upload_ids:
        composer: ContentComposer = st.content_composer
        rows = await composer.load_attachments(upload_ids)
        yield _status_event("parsing", files=[r.get("filename", "") for r in rows])

        blocks = await asyncio.gather(*(
            _process_one(
                composer, row,
                base_supports_vision=policy.base_supports_vision,
                vision_model_id=policy.vision_model_id,
            )
            for row in rows
        ))

        # 失败逐附件提示（用户须知道哪些图/文档没被看到）；全成功发一条汇总
        for b in blocks:
            if b.failed:
                yield _status_event(
                    "skipped", file=b.upload.get("filename", ""), reason=b.fail_reason
                )
        if all(not b.failed for b in blocks):
            yield _status_event("parsed", count=len(blocks))

        content = composer.render(user_query, list(blocks))
    if kbs:
        content = st.content_composer.with_kb_anchor(content, kbs)

    yield BuiltMessage(content=content, upload_ids=upload_ids)
