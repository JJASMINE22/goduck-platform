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
from app.models.schemas import (
    KnowledgeCapability,
    ResumeRequest,
    SendMessageRequest,
    ShellCapability,
)
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

    # 附件校验 + KB 锚点注入 + content 组装（plan §6.5）
    upload_ids: list[str] = []
    content: str | list = body.content
    needs_agent_cfg = bool(body.attachments) or bool(body.knowledge_base_ids)
    agent_row = await dbmod.get_agent(st.pool, session["agent_id"]) if needs_agent_cfg else None
    agent_cfg = (
        AgentConfig.model_validate(agent_row["config_json"]) if agent_row is not None else None
    )

    # ① 附件：能力门控 + upload_id 有效性/归属 + 多模态组装
    if body.attachments:
        assert agent_cfg is not None
        if not agent_cfg.allow_attachments:
            raise HTTPException(422, "该智能体未启用附件能力")
        for a in body.attachments:
            upload = await dbmod.get_upload(st.pool, uuid.UUID(a.upload_id))
            if upload is None or upload["session_id"] != session["id"]:
                raise HTTPException(403, f"无效附件: {a.upload_id}")
            upload_ids.append(a.upload_id)
        supports_vision = st.model_store.supports_vision(agent_row["model_id"])
        content = await st.content_composer.build(
            user_query=body.content,
            upload_ids=upload_ids,
            supports_vision=supports_vision,
        )

    # ② 知识库锚点：能力门控 + 选中 KB 存在性 + 语义暗示注入（不在此检索）
    # 工具发现交给 agent 原生 function-calling：无检索工具则按锚点原则自主降级。
    if body.knowledge_base_ids:
        assert agent_cfg is not None
        if st.knowledge_service is None:
            raise HTTPException(503, "知识库能力未启用")
        if not agent_cfg.allow_knowledge:
            raise HTTPException(422, "该智能体未启用知识库能力")
        kbs = await dbmod.get_kbs_by_ids(st.pool, body.knowledge_base_ids)
        if len(kbs) != len(body.knowledge_base_ids):
            raise HTTPException(404, "部分所选知识库不存在")
        content = st.content_composer.with_kb_anchor(content, kbs)

    adapter = st.stream_adapter

    async def event_gen():
        thread_id = session["thread_id"]
        # Withdraw race guard (plan R3): mark this thread active for the whole
        # generator lifetime so /withdraw refuses with 409 until the stream
        # truly finishes (finally runs even on client abort).
        st.stream_tracker.acquire(thread_id)
        try:
            async for ev in adapter.stream_message(
                agent,
                content,
                config,
                body.rubric,
                upload_ids=upload_ids,
                raw_content=body.content,
            ):
                yield ev.to_sse()
        finally:
            st.stream_tracker.release(thread_id)
            await svc.touch(session["id"])

    return EventSourceResponse(event_gen())


@router.get("/{session_id}/knowledge-capability", response_model=KnowledgeCapability)
async def get_knowledge_capability(session_id: uuid.UUID, request: Request):
    """前端据此渲染会话框 KB 选择器（DESIGN 准则 F2 能力驱动 UI）。

    allow_knowledge = 智能体配置允许 AND 全局 KB 能力已启用（kb_enabled）。
    kb_enabled=false 时直接 false（不查 agent 配置）。
    """
    from app.services.session_service import SessionService

    st = get_appstate(request)
    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    allow = False
    if st.knowledge_service is not None:  # kb_enabled
        agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
        if agent_row is not None:
            allow = AgentConfig.model_validate(agent_row["config_json"]).allow_knowledge
    return KnowledgeCapability(allow_knowledge=allow)


@router.get("/{session_id}/shell-capability", response_model=ShellCapability)
async def get_shell_capability(session_id: uuid.UUID, request: Request):
    """是否启用 shell 执行 —— 驱动会话页 shell 能力徽标（准则 F2 能力驱动 UI）。

    allow_shell = 该会话智能体主代理或任一子代理配置 allow_shell=true。
    （compiled 子代理需自身开启才有独立 shell 后端；dicted 子代理共享父
    后端，故随父代理生效。）
    """
    st = get_appstate(request)

    from app.services.session_service import SessionService

    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    allow = False
    agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
    if agent_row is not None:
        allow = _allows_shell_anywhere(AgentConfig.model_validate(agent_row["config_json"]))
    return ShellCapability(allow_shell=allow)


def _allows_shell_anywhere(cfg: AgentConfig) -> bool:
    """主代理或任一（递归）子代理声明了 allow_shell 即视为可执行 shell。"""
    if cfg.allow_shell:
        return True
    return any(_allows_shell_anywhere(sa) for sa in (cfg.subagents or []))


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
        thread_id = session["thread_id"]
        st.stream_tracker.acquire(thread_id)
        try:
            async for ev in adapter.stream_resume(agent, decisions, config):
                yield ev.to_sse()
        finally:
            st.stream_tracker.release(thread_id)
            await svc.touch(session["id"])

    return EventSourceResponse(event_gen())
