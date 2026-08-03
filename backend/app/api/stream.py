"""SSE streaming + HITL resume (plan §六 流式对话).

Two endpoints share the same SSE event protocol:
- POST /sessions/{id}/messages : send a message -> SSE
- POST /sessions/{id}/resume    : HITL decision -> SSE

Streams terminate explicitly with done / interrupt / error (plan §11.1).
Resume validates the graph is actually interrupted, else 409 Conflict.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.core.schema_utils import AgentConfig
from app.deps import SimpleUser, get_appstate
from app.models import db as dbmod
from app.models.schemas import ResumeRequest, SendMessageRequest
from app.services.hitl_resolver import HITLResolver

router = APIRouter(prefix="/sessions", tags=["流式对话"])


def _config_for(session: dict) -> dict:
    return {
        "configurable": {
            "thread_id": session["thread_id"],
            "langgraph_auth_user": SimpleUser(session["user_id"]),
        }
    }


async def _build_agent_or_409(st, agent_id: uuid.UUID):
    """Resolve the compiled agent, mapping model-unavailable -> 409.

    Agent create/update gate on model registration and model delete is blocked
    while referenced, so an unavailable model at session time means it was
    disabled later or is a legacy row without model_id — a conflict (409), not
    a server error (500). The underlying KeyError already carries a precise msg.
    """
    try:
        return await st.agent_registry.get_or_build(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _load_session_or_404(request: Request, session_id: uuid.UUID) -> dict:
    from app.services.session_service import SessionService

    svc = SessionService(get_appstate(request).pool)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session["status"] == "archived":
        raise HTTPException(status_code=409, detail="session archived")
    return session, svc


@router.post("/{session_id}/messages")
async def send_message(session_id: uuid.UUID, body: SendMessageRequest, request: Request):
    st = get_appstate(request)
    session, svc = await _load_session_or_404(request, session_id)
    config = _config_for(session)
    agent = await _build_agent_or_409(st, session["agent_id"])

    # 附件校验 + content 组装（plan §6.5）
    if body.attachments:
        agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
        agent_cfg = AgentConfig.model_validate(agent_row["config_json"])

        # 第 1 层：能力门控
        if not agent_cfg.allow_attachments:
            raise HTTPException(422, "该智能体未启用附件能力")

        # 第 2/3 层：upload_id 有效性 + 归属
        # 子类型放行由上传层 _ALLOWED_MIME 白名单单一可信源保证（kind=='file' ∈ {docx, pdf}），
        # 此处不再重复编码子类型规则；解析期类型兜底由 MCP parse_document 处理。
        upload_ids = []
        for a in body.attachments:
            upload = await dbmod.get_upload(st.pool, uuid.UUID(a.upload_id))
            if upload is None or upload["session_id"] != session["id"]:
                raise HTTPException(403, f"无效附件: {a.upload_id}")
            upload_ids.append(a.upload_id)

        # supports_vision 由该智能体绑定的注册模型决定（model 表字段）
        supports_vision = st.model_store.supports_vision(agent_row["model_id"])
        content = await st.content_composer.build(
            user_query=body.content,
            upload_ids=upload_ids,
            supports_vision=supports_vision,
        )
    else:
        content = body.content

    adapter = st.stream_adapter

    async def event_gen():
        try:
            async for ev in adapter.stream_message(agent, content, config, body.rubric):
                yield ev.to_sse()
        finally:
            await svc.touch(session["id"])

    return EventSourceResponse(event_gen())


@router.post("/{session_id}/resume")
async def resume(session_id: uuid.UUID, body: ResumeRequest, request: Request):
    st = get_appstate(request)
    session, svc = await _load_session_or_404(request, session_id)
    config = _config_for(session)
    agent = await _build_agent_or_409(st, session["agent_id"])

    resolver = HITLResolver()

    # Contract (plan §11.1): resume MUST verify interrupt state, else 409.
    intr = await resolver.assert_interrupted(agent, config)
    if intr is None:
        raise HTTPException(status_code=409, detail="session is not in an interrupt state")

    decisions = resolver.normalize_decisions(body.decisions)
    adapter = st.stream_adapter

    async def event_gen():
        try:
            async for ev in adapter.stream_resume(agent, decisions, config):
                yield ev.to_sse()
        finally:
            await svc.touch(session["id"])

    return EventSourceResponse(event_gen())
