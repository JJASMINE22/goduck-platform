"""上传接口 + 附件能力查询（plan §5）。

- POST /sessions/{id}/uploads : multipart 上传，服务端魔数判定 mime，存 MinIO + DB
- GET  /sessions/{id}/attachment-capability : 前端控制上传组件可见性
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.core.schema_utils import AgentConfig
from app.deps import get_appstate
from app.models import db as dbmod
from app.models.schemas import AttachmentCapability, UploadOut
from app.services.mime_utils import (
    detect_kind,
    detect_mime,
    extract_docx_meta,
    is_allowed_mime,
    is_docx,
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
    user_id: str = Form(...),
):
    """上传附件。前端只传文件流 + filename，mime 服务端魔数判定。"""
    st = _appstate(request)

    # ① 校验 session 存在 + 归属
    svc = SessionService(st.pool)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权操作该 session")

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
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件类型: {real_mime}，仅支持 image/* 与 docx",
        )

    # ④ 判定 kind + docx 轻量元数据
    kind = detect_kind(real_mime)
    page_count = None
    preview = None
    if is_docx(real_mime):
        page_count, preview = extract_docx_meta(raw)

    # ⑤ 存 MinIO（uuid bucket_key，防目录穿越）
    bucket_key = str(uuid.uuid4())
    try:
        st.minio_storage.put_object(bucket_key, raw, content_type=real_mime)
    except Exception as e:
        logger.exception("MinIO 存储失败")
        raise HTTPException(status_code=500, detail=f"存储失败: {e}")

    # ⑥ 存 DB 元数据
    upload = await dbmod.insert_upload(
        st.pool,
        session_id=session_id,
        user_id=user_id,
        bucket_key=bucket_key,
        filename=file.filename or "unnamed",
        mime=real_mime,
        kind=kind,
        size=len(raw),
        page_count=page_count,
        preview=preview,
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


@router.get("/{session_id}/attachment-capability", response_model=AttachmentCapability)
async def get_attachment_capability(session_id: uuid.UUID, request: Request):
    """前端据此渲染上传组件（支持的文件类型、大小限制）。"""
    st = _appstate(request)
    svc = SessionService(st.pool)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
    if agent_row is None:
        raise HTTPException(status_code=404, detail="agent not found")

    agent_cfg = AgentConfig.model_validate(agent_row["config_json"])
    supports_vision = st.model_store.supports_vision(agent_row["model_id"])

    return AttachmentCapability(
        allow_attachments=agent_cfg.allow_attachments,
        allow_image=supports_vision,
        allow_file=["docx"],
        max_size_mb=st.settings.attachment_max_size_mb,
    )
