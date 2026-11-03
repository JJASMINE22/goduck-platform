"""上传接口 + 附件能力查询（plan §5）。

- POST /sessions/{id}/uploads : multipart 上传，服务端魔数判定 mime，存 MinIO + DB
- GET  /sessions/{id}/attachment-capability : 前端控制上传组件可见性
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile

from app.api.stream import capability_agent_row, resolve_agent_choice_or_409
from app.deps import get_appstate, get_current_user
from app.models import db as dbmod
from app.models.schemas import AttachmentCapability, UploadOut
from app.services.attachment_policy import AttachmentPolicyService
from app.services.mime_utils import (
    allowed_file_extensions,
    detect_kind,
    detect_mime,
    extract_document_meta,
    is_allowed_mime,
)
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["附件管理"])


def _appstate(request: Request):
    return get_appstate(request)


@router.post("/{session_id}/uploads", response_model=UploadOut)
async def upload_file(
    session_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    agent_id: uuid.UUID | None = Form(default=None, description="当前选中智能体；缺省回落 last_agent_id"),
    user: dict = Depends(get_current_user),
):
    """上传附件。前端只传文件流 + filename，mime 服务端魔数判定，user_id
    取自登录令牌（客户端不再传）。

    agent_id 弱绑定解析（§4.4）：附件归属 session（与 agent 无关），能力
    谓词按"显式 > last"选定的当回合智能体派生——上传时可传 A 允许图片、
    发送时已切 B 的窗口由发送门纵深兜底（422）。
    """
    st = _appstate(request)

    # ① 校验 session 存在 + 归属（非本人一律 404，不暴露存在性）
    svc = SessionService(st.pool)
    session = await svc.get(session_id)
    if session is None or session["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")

    # ② 读取字节 + 大小校验
    raw = await file.read()
    max_bytes = st.settings.attachment_max_size_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小限制 {st.settings.attachment_max_size_mb}MB",
        )

    # ③ 服务端判定真实 mime（魔数 > 扩展名 > 客户端）
    real_mime = detect_mime(raw, file.filename, file.content_type)
    if not is_allowed_mime(real_mime):
        # 支持清单由白名单动态派生（mime_utils 单一来源），文案随白名单自动跟随
        raise HTTPException(
            status_code=415,
            detail=(
                f"不支持的文件类型: {real_mime}，"
                f"仅支持 image/* 与 {'、'.join(allowed_file_extensions())}"
            ),
        )

    # ④ 智能体级策略门（plan §3.2）：附件总开关 + 图像能力（基座视觉 OR 下挂
    #    视觉模型，动态派生——管理员换模型/禁下挂后立即收紧）。显式 422 而非
    #    上传后静默丢弃：用户当场知道该智能体收不了这类文件。
    chosen_id = await resolve_agent_choice_or_409(st, session, agent_id)
    policy = await AttachmentPolicyService(st).for_agent_id(chosen_id)
    if not policy.allow_attachments:
        raise HTTPException(status_code=422, detail="该智能体未启用附件能力")
    kind = detect_kind(real_mime)
    if kind == "image" and not policy.allow_image:
        raise HTTPException(
            status_code=422,
            detail="该智能体不支持图像附件（基座模型无视觉能力且未下挂视觉模型）",
        )

    # ⑤ 判定文档轻量元数据（按 mime 分派：docx 段落数 / pdf 页数）
    page_count, preview = extract_document_meta(raw, real_mime)

    # ⑥ 存 MinIO（bucket_key == upload id：无 DB 的 wrapper 可凭 upload_id 直取对象）
    new_id = uuid.uuid4()
    bucket_key = str(new_id)
    try:
        st.minio_storage.put_object(bucket_key, raw, content_type=real_mime)
    except Exception as e:
        logger.exception("MinIO 存储失败")
        raise HTTPException(status_code=500, detail=f"存储失败: {e}")

    # ⑦ 存 DB 元数据
    upload = await dbmod.insert_upload(
        st.pool,
        session_id=session_id,
        user_id=user["id"],
        bucket_key=bucket_key,
        filename=file.filename or "unnamed",
        mime=real_mime,
        kind=kind,
        size=len(raw),
        page_count=page_count,
        preview=preview,
        upload_id=new_id,
    )

    return UploadOut(
        upload_id=upload["id"],
        kind=upload["kind"],
        filename=upload["filename"],
        mime=upload["mime"],
        size=upload["size"],
        page_count=upload["page_count"],
        preview=upload["preview"],
    )


@router.get("/{session_id}/uploads/{upload_id}/preview")
async def preview_upload(
    session_id: uuid.UUID,
    upload_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """返回附件字节供前端缩略图渲染（仅 image；文档类附件无二进制预览 → 404，前端回退图标）。"""
    st = _appstate(request)
    upload = await dbmod.get_upload(st.pool, upload_id)
    if upload is None or upload["session_id"] != session_id:
        raise HTTPException(status_code=404, detail="upload not found")
    if upload["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="upload not found")
    if upload["kind"] != "image":
        raise HTTPException(status_code=404, detail="非图片附件无二进制预览")
    raw = await asyncio.to_thread(st.minio_storage.get_object, upload["bucket_key"])
    return Response(content=raw, media_type=upload["mime"])


@router.get("/{session_id}/attachment-capability", response_model=AttachmentCapability)
async def get_attachment_capability(
    session_id: uuid.UUID,
    request: Request,
    agent_id: uuid.UUID | None = Query(default=None, description="当前选中智能体；缺省回落 last_agent_id"),
    user: dict = Depends(get_current_user),
):
    """前端据此渲染上传组件（支持的文件类型、大小限制）。

    策略经 AttachmentPolicyService 动态派生（单一真相）：
    - allow_attachments：智能体配置开关；
    - allow_image = 基座 supports_vision OR 下挂视觉模型当前可用
      （原 `endswith("__image_understand")` 工具名启发式已随 MCP 解析链路退役）；
    - allow_file：mime 白名单派生（mime_utils 单一来源）。
    解析落空（未选/悬空/选择失效）→ 全关降级（capability_agent_row，§4.4）。
    """
    st = _appstate(request)
    svc = SessionService(st.pool)
    session = await svc.get(session_id)
    if session is None or session["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")

    agent_row = await capability_agent_row(st, session, agent_id)
    policy = AttachmentPolicyService(st).for_agent_row(agent_row)
    return AttachmentCapability(
        allow_attachments=policy.allow_attachments,
        allow_image=policy.allow_image,
        allow_file=policy.allow_file,
        max_size_mb=policy.max_size_mb,
    )
