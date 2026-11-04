"""向量模型（embedding）注册管理：CRUD + 连通探测（镜像 /models）。

预检/守卫在 EmbeddingService（类型化异常）。变更后刷新 EmbeddingStore 并
失效 vector_factory 派生缓存，免重启生效；仅 kb_enabled=true 时挂载。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import get_appstate, require_manager
from app.models.schemas import (
    EmbeddingCreate,
    EmbeddingOut,
    EmbeddingTestRequest,
    EmbeddingUpdate,
)
from app.services.embedding import build_from_row
from app.services.embedding_service import EmbeddingReferencedError, EmbeddingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embeddings", tags=["向量模型管理"])


def _appstate(request: Request):
    st = get_appstate(request)
    if st.embedding_store is None:
        raise HTTPException(status_code=503, detail="知识库能力未启用")
    return st


def _service(request: Request) -> EmbeddingService:
    return EmbeddingService(get_appstate(request).pool)


async def _sync_caches(st, embedding_id: uuid.UUID) -> None:
    """注册行变更 → 注册表换血 + 派生 store 失效（免重启）。"""
    await st.embedding_store.refresh()
    if st.vector_factory is not None:
        st.vector_factory.invalidate_embedding(embedding_id)


@router.get("", response_model=list[EmbeddingOut])
async def list_embeddings(request: Request):
    """List registered embeddings (api_key never returned)."""
    return await _service(request).list_embeddings()


@router.post("", response_model=EmbeddingOut, status_code=201)
async def create_embedding(
    body: EmbeddingCreate,
    request: Request,
    user: dict = Depends(require_manager),
):
    st = _appstate(request)
    try:
        row = await _service(request).create(
            name=body.name,
            provider_kind=body.provider_kind,
            model_type=body.model_type,
            api_key=body.api_key,
            base_url=body.base_url,
            dims=body.dims,
            batch_size=body.batch_size,
            timeout=body.timeout,
            check_ctx_length=body.check_ctx_length,
            enabled=body.enabled,
            creator=user["nickname"],
        )
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"向量模型名 '{body.name}' 已存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await st.embedding_store.refresh()
    return row


@router.put("/{embedding_id}", response_model=EmbeddingOut)
async def update_embedding(
    embedding_id: uuid.UUID,
    body: EmbeddingUpdate,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    st = _appstate(request)
    try:
        row = await _service(request).update(
            embedding_id,
            name=body.name,
            provider_kind=body.provider_kind,
            model_type=body.model_type,
            api_key=body.api_key,
            base_url=body.base_url,
            dims=body.dims,
            batch_size=body.batch_size,
            timeout=body.timeout,
            check_ctx_length=body.check_ctx_length,
            enabled=body.enabled,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="embedding not found")
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"向量模型名 '{body.name}' 已被占用")
    except EmbeddingReferencedError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e) + "；请先为这些库换绑向量模型",
                "referencing_kbs": [{"id": str(k["id"]), "name": k["name"]} for k in e.referencing],
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _sync_caches(st, embedding_id)
    return row


@router.delete("/{embedding_id}")
async def delete_embedding(
    embedding_id: uuid.UUID, request: Request, _admin: dict = Depends(require_manager)
):
    st = _appstate(request)
    try:
        await _service(request).delete(embedding_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="embedding not found")
    except EmbeddingReferencedError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e) + "；请先为这些库换绑向量模型",
                "referencing_kbs": [{"id": str(k["id"]), "name": k["name"]} for k in e.referencing],
            },
        )
    await _sync_caches(st, embedding_id)
    return {"ok": True}


@router.post("/test")
async def test_embedding(
    body: EmbeddingTestRequest, request: Request, _admin: dict = Depends(require_manager)
):
    """Pre-save connectivity probe: embed one probe text, report real dims.

    Does NOT persist. Avoids saving a misconfigured endpoint.
    """
    try:
        vec = await build_from_row({
            "model_type": body.model_type,
            "api_key": body.api_key,
            "base_url": body.base_url,
            "dims": None,  # 探实测真实维度，不做 MRL 截断
            "batch_size": 1,
            "timeout": body.timeout,
            "check_ctx_length": body.check_ctx_length,
        }).aembed_query("dim-probe")
        return {"ok": True, "dims": len(vec)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
