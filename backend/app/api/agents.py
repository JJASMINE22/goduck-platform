"""Agent CRUD + validate + topology preview + history restore (plan §3)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.deps import get_appstate
from app.models.schemas import (
    AgentCreate,
    AgentDetail,
    AgentHistoryItem,
    AgentOut,
    AgentUpdate,
    RestoreRequest,
    ValidateRequest,
    ValidateResponse,
)
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["智能体管理"])


def _service(request: Request) -> AgentService:
    return AgentService(get_appstate(request).pool)


def _with_mermaid(agent: dict, svc: AgentService) -> dict:
    agent = dict(agent)
    agent["active_mermaid"] = svc.render_mermaid(agent["config_json"])
    return agent


@router.post("/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest, request: Request):
    """Dry-run: validate config + return mermaid preview (no persistence)."""
    svc = _service(request)
    valid, errs = svc.validate_config(body.config)
    return ValidateResponse(
        valid=valid,
        errors=errs,
        mermaid=svc.render_mermaid(body.config) if valid else None,
    )


@router.post("", response_model=AgentDetail)
async def create_agent(body: AgentCreate, request: Request):
    st = get_appstate(request)
    svc = _service(request)
    # 校验 model_id 已注册且可用
    if not st.model_store.has(body.model_id):
        raise HTTPException(status_code=404, detail=f"model {body.model_id} 未注册或已禁用")
    try:
        agent = await svc.create(body.name, body.description, body.config, body.model_id, body.creator)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"agent '{body.name}' already exists")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # first build into registry (create: must not pre-exist)
    await st.agent_registry.get_or_build(agent["id"])
    return _with_mermaid(agent, svc)


@router.put("/{agent_id}", response_model=AgentDetail)
async def update_agent(agent_id: uuid.UUID, body: AgentUpdate, request: Request):
    st = get_appstate(request)
    svc = _service(request)
    # 校验 model_id 已注册且可用（换模型时同样校验）
    if not st.model_store.has(body.model_id):
        raise HTTPException(status_code=404, detail=f"model {body.model_id} 未注册或已禁用")
    try:
        agent = await svc.update(
            agent_id, body.config, body.model_id, body.description, body.updater, body.name
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"agent name '{body.name}' already exists")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # update: must pre-exist in registry -> invalidate then rebuild
    st.agent_registry.invalidate(agent["id"])
    await st.agent_registry.get_or_build(agent["id"])
    return _with_mermaid(agent, svc)


@router.get("", response_model=list[AgentOut])
async def list_agents(request: Request, include_archived: bool = Query(False)):
    return await _service(request).list_agents(include_archived=include_archived)


@router.get("/{agent_id}", response_model=AgentDetail)
async def get_agent(agent_id: uuid.UUID, request: Request):
    svc = _service(request)
    agent = await svc.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _with_mermaid(agent, svc)


@router.get("/{agent_id}/topology.mmd", response_class=PlainTextResponse)
async def get_topology_mmd(agent_id: uuid.UUID, request: Request):
    svc = _service(request)
    agent = await svc.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    mermaid = svc.render_mermaid(agent["config_json"]) or ""
    return PlainTextResponse(mermaid, media_type="text/plain")


@router.get("/{agent_id}/history", response_model=list[AgentHistoryItem])
async def list_history(agent_id: uuid.UUID, request: Request):
    return await _service(request).list_history(agent_id)


@router.post("/{agent_id}/restore", response_model=AgentDetail)
async def restore_agent(agent_id: uuid.UUID, body: RestoreRequest, request: Request):
    st = get_appstate(request)
    svc = _service(request)
    # validate the snapshot's model BEFORE the destructive restore commit:
    # history.model_id has no FK — legacy snapshots may be NULL or stale, and
    # restore_agent purges sessions+checkpoints before any later build can fail.
    snapshot = await svc.history_snapshot(agent_id, body.history_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="history not found")
    candidate = snapshot["model_id"]
    if candidate is None or not st.model_store.has(candidate):
        raise HTTPException(
            status_code=409,
            detail="历史快照绑定的模型不可用（未注册/已禁用），请先注册或更换模型后再还原",
        )
    agent = await svc.restore(agent_id, body.history_id, body.operator)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent or history not found")
    st.agent_registry.invalidate(agent["id"])
    await st.agent_registry.get_or_build(agent["id"])
    return _with_mermaid(agent, svc)


@router.delete("/{agent_id}", response_model=AgentOut)
async def archive_agent(agent_id: uuid.UUID, request: Request):
    st = get_appstate(request)
    svc = _service(request)
    agent = await svc.archive(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    st.agent_registry.invalidate(agent["id"])
    return agent


@router.post("/{agent_id}/purge", status_code=204)
async def purge_agent(agent_id: uuid.UUID, request: Request):
    """Hard delete an agent (physical) + cascade sessions/checkpoints/history.

    Irreversible. Two-step delete: archive first (recoverable), purge to remove
    for good. Registry cache is invalidated.
    """
    st = get_appstate(request)
    svc = _service(request)
    deleted = await svc.delete(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="agent not found")
    st.agent_registry.invalidate(agent_id)
    return None


@router.get("/{agent_id}/export.yaml", response_class=PlainTextResponse)
async def export_yaml(agent_id: uuid.UUID, request: Request):
    svc = _service(request)
    yaml_text = await svc.export_yaml(agent_id)
    if yaml_text is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return PlainTextResponse(yaml_text, media_type="text/yaml")
