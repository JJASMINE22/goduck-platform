"""SSE streaming + HITL resume (plan §六 流式对话).

Two endpoints share the same SSE event protocol:
- POST /sessions/{id}/messages : send a message -> SSE
- POST /sessions/{id}/resume    : HITL decision -> SSE

Streams terminate explicitly with done / interrupt / error (plan §11.1).
Resume validates the graph is actually interrupted, else 409 Conflict.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse

from app.streaming.events import SSEEvent

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
from app.services.message_content import (
    BuiltMessage,
    MessageContentError,
    assemble_message_content_stream,
    validate_message_content,
)

logger = logging.getLogger(__name__)

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


async def _persist_usage(pool, session_id: uuid.UUID, payload: dict) -> None:
    """Best-effort mirror one `usage` event into session.usage_totals.

    统计入库绝不允许打断对话流：写库失败仅告警（下次 /sessions 回读时前端
    以服务端真值兜底）。按事件即时入库（而非流末一次性）：中止/断连路径的
    已完成 LLM 调用同样计入。语音 WS 隧道（voice.py）复用本函数。
    """
    try:
        await dbmod.add_session_usage(pool, session_id, payload)
    except Exception:  # noqa: BLE001
        logger.warning("persist usage for session %s failed (skipped)", session_id, exc_info=True)


def _refuse_cron_session(session: dict, *, action: str) -> None:
    """任务会话（kind='cron'）只读守卫：内容由调度器注入，用户不可主动发言/
    追问/回撤（产品决策）。HITL 审批（resume）是唯一放行的交互。"""
    if session.get("kind") == "cron":
        raise HTTPException(
            status_code=409,
            detail=f"任务会话由定时调度驱动，不支持{action}（HITL 审批除外）",
        )


@router.post("/{session_id}/messages")
async def send_message(session_id: uuid.UUID, body: SendMessageRequest, request: Request):
    st = get_appstate(request)
    session, svc = await _load_session_or_404(request, session_id)
    _refuse_cron_session(session, action="手动发送消息")
    config = _config_for(session)
    agent = await _build_agent_or_409(st, session["agent_id"])

    # 附件/KB 校验（流外，plan §6.5）—— 与语音 WS 共享同一校验器：能力门控 422 /
    # 附件归属 403 / KB 不存在 404 / 503 以 HTTP 状态码尽早暴露；组装（含图像
    # 预消费与 status 事件）移入 event_gen，withdraw 竞态由 tracker 全程覆盖。
    try:
        await validate_message_content(
            st,
            session,
            attachments=body.attachments,
            knowledge_base_ids=body.knowledge_base_ids,
        )
    except MessageContentError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    adapter = st.stream_adapter

    async def event_gen():
        thread_id = session["thread_id"]
        # Withdraw race guard (plan R3)：从预消费（附件解析）开始即占用，
        # 解析期间的 withdraw 同样被 409 拒绝。
        st.stream_tracker.acquire(thread_id)
        try:
            built: BuiltMessage | None = None
            async for item in assemble_message_content_stream(
                st,
                session,
                user_query=body.content,
                attachments=body.attachments,
                knowledge_base_ids=body.knowledge_base_ids,
            ):
                if isinstance(item, SSEEvent):
                    yield item.to_sse()  # status: 正在解析附件… / 已跳过…
                else:
                    built = item
            assert built is not None  # assemble 生成器必以 BuiltMessage 收尾

            gen = adapter.stream_message(
                agent,
                built.content,
                config,
                body.rubric,
                upload_ids=built.upload_ids,
                raw_content=body.content,
                kb_ids=[str(k) for k in body.knowledge_base_ids],
            )
            try:
                async for ev in gen:
                    # token 用量即时入库（best-effort，失败不打断流；见 _persist_usage）
                    if ev.type == "usage":
                        await _persist_usage(st.pool, session["id"], ev.payload)
                    yield ev.to_sse()
            finally:
                # 客户端中止（abort fetch → FastAPI 取消本生成器）：确定性关闭 adapter
                # 生成器链，避免悬挂在 yield 处等 GC 终结器 athrow（与语音 WS 隧道同修）
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await gen.aclose()
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


@router.get("/{session_id}/interrupt")
async def get_pending_interrupt(session_id: uuid.UUID, request: Request):
    """待审批 HITL 查询（会话重入恢复，plan §5.3 补全）。

    待审批状态在前端仅存活于 live SSE 流内：刷新/切会话即丢失审批卡，
    图却仍暂停在 interrupt（悬空工具卡永远「执行中」，且用户发新消息会
    静默取消待审批）。本端点返回 checkpointer 真值——与 interrupt SSE 事件
    同一 payload 形状（action_requests + review_configs），前端据此重建
    interrupted 状态与审批卡。无待审批 → 204。

    只读 aget_state，不占用 stream_tracker；resume 端点同源校验（409 契约
    不变），两端点天然一致。
    """
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    config = _config_for(session)
    agent = await _build_agent_or_409(st, session["agent_id"])
    intr = await HITLResolver().assert_interrupted(agent, config)
    # 非字典/空值（如裸 interrupt() 原语）无 action_requests，前端守卫会跳过；
    # 此处一并 204，保持「有返回即可渲染审批卡」的简单契约
    if not intr:
        return Response(status_code=204)
    return intr


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
    is_cron_session = session.get("kind") == "cron"

    async def event_gen():
        thread_id = session["thread_id"]
        st.stream_tracker.acquire(thread_id)
        gen = adapter.stream_resume(agent, decisions, config)
        try:
            async for ev in gen:
                if ev.type == "usage":
                    await _persist_usage(st.pool, session["id"], ev.payload)
                # 任务会话：审批流的终态同步回任务状态（done→恢复触发、
                # interrupt→继续挂起、error→计失败），不产生 cron_run 行。
                if is_cron_session and ev.type in ("done", "interrupt", "error"):
                    await _sync_cron_job_status(st.pool, session["id"], ev.type)
                yield ev.to_sse()
        finally:
            st.stream_tracker.release(thread_id)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await gen.aclose()
            await svc.touch(session["id"])

    return EventSourceResponse(event_gen())


async def _sync_cron_job_status(pool, session_id: uuid.UUID, final_event: str) -> None:
    """HITL 审批 resume 完成后，把结局映射回绑定任务的 last_status（best-effort）。"""
    from app.services.cron_scheduler import _STATUS_BY_EVENT

    mapped = _STATUS_BY_EVENT.get(final_event)
    if mapped is None:
        return
    try:
        await dbmod.sync_cron_job_status(pool, session_id, mapped)
    except Exception:  # noqa: BLE001
        logger.warning("sync cron job status after resume failed (session %s)", session_id)
