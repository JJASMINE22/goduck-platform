"""Registered model ops: CRUD + connectivity test (mirrors /mcp).

Validation lives in ModelService (explicit pre-checks + typed exceptions),
symmetric with AgentService. On any mutation we refresh ModelStore so agents
pick up key rotation / model swaps without a restart.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.deps import get_appstate
from app.models.schemas import ModelCreate, ModelOut, ModelTestRequest, ModelUpdate
from app.services.model_service import ModelInUseError, ModelService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["模型管理"])


def _appstate(request: Request):
    return get_appstate(request)


def _service(request: Request) -> ModelService:
    return ModelService(get_appstate(request).pool)


def _invalidate_agents(st) -> None:
    """Best-effort: invalidate cached agents so they rebuild against changed models."""
    if st.agent_registry is not None:
        st.agent_registry.invalidate(None)


@router.get("", response_model=list[ModelOut])
async def list_models(request: Request):
    """List registered models (api_key never returned)."""
    return await _service(request).list_models()


@router.post("", response_model=ModelOut)
async def create_model(body: ModelCreate, request: Request):
    st = _appstate(request)
    svc = _service(request)
    try:
        row = await svc.create(
            name=body.name,
            provider_kind=body.provider_kind,
            model_type=body.model_type,
            api_key=body.api_key,
            base_url=body.base_url,
            supports_vision=body.supports_vision,
            enabled=body.enabled,
        )
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"模型名 '{body.name}' 已存在")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await st.model_store.refresh()
    return row


@router.put("/{model_id}", response_model=ModelOut)
async def update_model(model_id: uuid.UUID, body: ModelUpdate, request: Request):
    st = _appstate(request)
    svc = _service(request)
    try:
        row = await svc.update(
            model_id,
            name=body.name,
            provider_kind=body.provider_kind,
            model_type=body.model_type,
            api_key=body.api_key,
            base_url=body.base_url,
            supports_vision=body.supports_vision,
            enabled=body.enabled,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="model not found")
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"模型名 '{body.name}' 已被其它模型占用")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await st.model_store.refresh()
    # 引用该模型的智能体需重建以跟随新的 key/model_type
    _invalidate_agents(st)
    return row


@router.delete("/{model_id}")
async def delete_model(model_id: uuid.UUID, request: Request):
    st = _appstate(request)
    svc = _service(request)
    try:
        await svc.delete(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="model not found")
    except ModelInUseError as e:
        # 阻断删除：仍有智能体引用，提示用户先手动解绑（不级联删 agent）
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"{len(e.referencing)} 个智能体仍在使用该模型，请先为其更换基座模型",
                "referencing_agents": [{"id": str(a["id"]), "name": a["name"]} for a in e.referencing],
            },
        )
    await st.model_store.refresh()
    return {"ok": True}


@router.post("/test")
async def test_model(body: ModelTestRequest, request: Request):
    """Pre-save connectivity probe: build a client from raw params and ping.

    Does NOT persist. Avoids saving a misconfigured model.
    """
    from app.services.model_store import build_chat_model

    try:
        client = build_chat_model(
            body.provider_kind, body.model_type, body.api_key, body.base_url
        )
        # minimal non-streaming round-trip
        await client.ainvoke("ping")
        return {"ok": True, "model_type": body.model_type}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
