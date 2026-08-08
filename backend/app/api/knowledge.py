"""知识库管理接口（plan §5.8）。

- POST/GET/PUT/DELETE /knowledge-bases
- POST /knowledge-bases/{id}/documents (multipart) → 注册 + 后台入库
- GET  /knowledge-bases/{id}/documents
- DELETE /knowledge-bases/{id}/documents/{doc_id}
- POST /knowledge-bases/{id}/documents/{doc_id}/reindex
- GET  /knowledge-bases/{id}/search?q=&k= （管理面调试）

kb_enabled=false 时全部返回 503。仅收 pdf/docx。
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from app.deps import get_appstate
from app.models.schemas import (
    KbDocumentOut,
    KbSearchResult,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.services.knowledge_service import DimensionMismatchError
from app.services.mime_utils import detect_mime, is_docx, is_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


def _appstate(request: Request):
    st = get_appstate(request)
    if not st.settings.kb_enabled or st.knowledge_service is None:
        raise HTTPException(status_code=503, detail="知识库能力未启用")
    return st


def _service(request: Request):
    return _appstate(request).knowledge_service


# ============================ KB CRUD =======================================

@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(request: Request):
    return await _service(request).list_kbs()


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(body: KnowledgeBaseCreate, request: Request):
    svc = _service(request)
    try:
        row = await svc.create_kb(body.name, body.description, body.creator)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"知识库名 '{body.name}' 已存在")
    return row


@router.put("/{kb_id}", response_model=KnowledgeBaseOut)
async def update_kb(kb_id: uuid.UUID, body: KnowledgeBaseUpdate, request: Request):
    svc = _service(request)
    try:
        row = await svc.update_kb(kb_id, body.name, body.description)
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"知识库名 '{body.name}' 已被占用")
    return row


@router.delete("/{kb_id}")
async def delete_kb(kb_id: uuid.UUID, request: Request):
    svc = _service(request)
    try:
        await svc.delete_kb(kb_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return {"ok": True}


# ============================ Documents =====================================

@router.get("/{kb_id}/documents", response_model=list[KbDocumentOut])
async def list_documents(kb_id: uuid.UUID, request: Request):
    return await _service(request).list_documents(kb_id)


@router.post("/{kb_id}/documents", response_model=KbDocumentOut, status_code=201)
async def upload_document(
    kb_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
):
    """注册文档并投递后台入库。前端只传文件流，mime 服务端魔数判定。"""
    st = _appstate(request)
    svc = st.knowledge_service

    raw = await file.read()
    max_bytes = st.settings.attachment_max_size_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"文件超过大小限制 {st.settings.attachment_max_size_mb}MB"
        )

    # KB 仅收 pdf/docx（魔数 > 扩展名 > 客户端）
    real_mime = detect_mime(raw, file.filename, file.content_type)
    if not (is_pdf(real_mime) or is_docx(real_mime)):
        raise HTTPException(status_code=415, detail=f"知识库仅支持 pdf/docx，收到 {real_mime}")

    # 注册（MinIO 先行 → PG pending）；随后 fire-and-forget 入库
    try:
        doc = await svc.register_document(
            kb_id=kb_id,
            filename=file.filename or "unnamed",
            mime=real_mime,
            raw=raw,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"该知识库下已存在同名文档「{file.filename or 'unnamed'}」，请先删除后再上传",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # KB_STORE_MINIO=false → 透传内存字节；否则 ingest 从 MinIO 取
    raw_for_ingest = raw if not st.settings.kb_store_minio else None
    st.ingest_pool.submit(kb_id, doc["id"], raw_for_ingest)
    return doc


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request):
    svc = _service(request)
    try:
        await svc.delete_document(kb_id, doc_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")
    return {"ok": True}


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=KbDocumentOut)
async def reindex_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request):
    """重建文档索引（需 MinIO 留底）：删旧 chunk 后重新入库。"""
    st = _appstate(request)
    svc = st.knowledge_service
    try:
        await svc.reindex_document(kb_id, doc_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    doc = await svc.get_document(doc_id)
    return doc


# ============================ Search (debug) ================================

@router.get("/{kb_id}/search", response_model=list[KbSearchResult])
async def search_kb(
    kb_id: uuid.UUID,
    request: Request,
    q: str = Query(..., min_length=1),
    k: int = Query(default=5, ge=1, le=50),
):
    """管理面调试检索（kb_ids 限定单库）。生产检索走 mcp-servers 的 kb_search 工具。"""
    svc = _service(request)
    try:
        return await svc.search([kb_id], q, k=k)
    except DimensionMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
