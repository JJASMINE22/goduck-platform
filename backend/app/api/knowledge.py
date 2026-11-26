"""知识库管理接口（plan §5.8）。

- POST/GET/PUT/DELETE /knowledge-bases（创建/更新可带 dims；维度变更守卫见 service）
- POST /knowledge-bases/{id}/documents (multipart) → 仅注册（pending=待解析）
- POST /knowledge-bases/{id}/documents/{doc_id}/parse → 手动解析（切块参数随请求）
- GET  /knowledge-bases/{id}/documents
- DELETE /knowledge-bases/{id}/documents/{doc_id}
- POST /knowledge-bases/{id}/documents/{doc_id}/reindex（沿用已存参数重建）
- GET  /knowledge-bases/{id}/search?q=&k= （管理面调试）

kb_enabled=false 时全部返回 503。仅收 pdf/docx。上传不自动解析（延迟手动触发），
故要求 KB_STORE_MINIO=true（解析时从留底取字节）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile

from app.deps import get_appstate, require_manager
from app.models import db as dbmod
from app.models.schemas import (
    KbAnchorCard,
    KbAnchorCardsRequest,
    KbDocumentOut,
    KbParseRequest,
    KbParserEngineOut,
    KbSearchResult,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
)
from app.services.knowledge_service import (
    DimensionMismatchError,
    DocumentBusyError,
    KbDocsExistError,
)
from app.services.mime_utils import detect_mime, is_docx, is_pdf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


def _appstate(request: Request):
    st = get_appstate(request)
    if not st.settings.kb_enabled or st.knowledge_service is None:
        raise HTTPException(status_code=503, detail="知识库能力未启用")
    return st


def _uses_kb_id(node, kb_id: str) -> bool:
    """config_json 树中任意层级的 kb_ids 是否引用了该知识库（递归全扫描）。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "kb_ids" and isinstance(v, list) and kb_id in v:
                return True
            if _uses_kb_id(v, kb_id):
                return True
    elif isinstance(node, list):
        return any(_uses_kb_id(item, kb_id) for item in node)
    return False


async def _agents_referencing_kb(st, kb_id: uuid.UUID) -> list[dict]:
    """知识集引用该库的智能体（id+name），供删除 409 明细展示。"""
    target = str(kb_id)
    hits: list[dict] = []
    for a in await dbmod.list_agent_configs(st.pool):
        if _uses_kb_id(a["config_json"], target):
            hits.append({"id": str(a["id"]), "name": a["name"]})
    return hits


def _service(request: Request):
    return _appstate(request).knowledge_service


# ============================ KB CRUD =======================================

@router.get("", response_model=list[KnowledgeBaseOut])
async def list_kbs(request: Request):
    return await _service(request).list_kbs()


@router.post("", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(
    body: KnowledgeBaseCreate,
    request: Request,
    user: dict = Depends(require_manager),
):
    svc = _service(request)
    try:
        row = await svc.create_kb(
            body.name, body.description, user["nickname"],
            dims=body.dims, embedding_id=body.embedding_id,
        )
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"知识库名 '{body.name}' 已存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row


@router.put("/{kb_id}", response_model=KnowledgeBaseOut)
async def update_kb(
    kb_id: uuid.UUID,
    body: KnowledgeBaseUpdate,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    svc = _service(request)
    try:
        row = await svc.update_kb(
            kb_id, body.name, body.description,
            dims=body.dims, force=body.force, embedding_id=body.embedding_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"知识库名 '{body.name}' 已被占用")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KbDocsExistError as e:
        # 维度变更守卫：前端据此弹二次确认（确认后 force=true 重试）
        raise HTTPException(
            status_code=409,
            detail={"message": str(e), "doc_count": e.doc_count, "needs_force": True},
        )
    return row


@router.delete("/{kb_id}")
async def delete_kb(
    kb_id: uuid.UUID, request: Request, _admin: dict = Depends(require_manager)
):
    # 阻断删除：仍有智能体把该库绑进知识集（kb v2 引用扫描，对齐 /mcp 的
    # tool_names 409 模式——绝不级联改 agent 配置）。先查后删，删除不可逆。
    referencing = await _agents_referencing_kb(_appstate(request), kb_id)
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(referencing)} 个智能体的知识集仍引用该知识库，"
                    "请先在其配置中移除该库"
                ),
                "referencing_agents": referencing,
            },
        )
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
    _admin: dict = Depends(require_manager),
):
    """注册文档（仅上传留底，不解析）：前端只传文件流，mime 服务端魔数判定。

    解析由用户显式触发（POST .../parse，可带切块参数）；故要求 KB_STORE_MINIO=true
    （延迟解析依赖留底取字节）。
    """
    st = _appstate(request)
    svc = st.knowledge_service
    if not st.settings.kb_store_minio:
        raise HTTPException(
            status_code=400,
            detail="手动解析模式要求 KB_STORE_MINIO=true（上传即留底）；当前配置不支持",
        )

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

    # 注册（MinIO 先行 → PG pending=待解析）；解析由用户手动触发
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
    return doc


@router.post("/{kb_id}/documents/{doc_id}/parse", response_model=KbDocumentOut)
async def parse_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: KbParseRequest,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    """手动解析：校验 + 持久化切块参数 → 后台入库（幂等，重建场景同样适用）。

    参数缺省时沿用文档已存参数（首次解析则用全局默认）。
    """
    st = _appstate(request)
    svc = st.knowledge_service
    try:
        doc = await svc.parse_document(
            kb_id, doc_id,
            chunk_size=body.chunk_size, chunk_overlap=body.chunk_overlap,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")
    except DocumentBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    st.ingest_pool.submit(kb_id, doc_id, None)
    return doc


@router.delete("/{kb_id}/documents/{doc_id}")
async def delete_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    svc = _service(request)
    try:
        await svc.delete_document(kb_id, doc_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="document not found")
    return {"ok": True}


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=KbDocumentOut)
async def reindex_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    """重建文档索引（沿用已存解析参数；需 MinIO 留底）：删旧 chunk 后重新入库。"""
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

@router.get("/parser-engine", response_model=KbParserEngineOut)
async def get_parser_engine(request: Request):
    """解析引擎状态下发（C1）：前端据此切换解析面板双轨语义（docling=隐藏
    滑窗参数，改释为结构单元预算）。"""
    st = _appstate(request)
    return KbParserEngineOut(
        engine=st.settings.kb_parser_engine,
        unit_max_chars=st.settings.kb_unit_max_chars,
    )


@router.post("/anchor-cards", response_model=list[Optional[KbAnchorCard]])
async def get_anchor_cards(body: KbAnchorCardsRequest, request: Request):
    """kb_search 工具结果的锚定卡批量补全（聊天侧渲染通道）。

    输入 = 工具结果 hits 的 (kb_id, doc_id, chunk_index) 键组；输出等长对位，
    缺失/无锚位为 null。kb_search（mcp-servers，面向 LLM）只回文本，锚定
    元数据由前端持键组在此懒取：本端点持有 MinIO 出预签名 doc_url，懒签
    规避工具结果持久化后 URL 过期（1h）。
    """
    svc = _service(request)
    return await svc.anchor_cards(
        [
            {
                "kb_id": it.kb_id,
                "doc_id": it.doc_id,
                "chunk_index": it.chunk_index,
            }
            for it in body.items
        ]
    )


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
