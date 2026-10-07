"""SSE streaming + HITL resume + run observe/cancel（plan §六 流式对话 + sse-run-detach-reattach）。

四个端点共用同一 SSE 事件协议：
- POST /sessions/{id}/messages : 发消息 → 启动 run → 以订阅者身份返回 SSE
- POST /sessions/{id}/resume    : HITL 决策 → 续跑 run → SSE（同上）
- GET  /sessions/{id}/stream    : 观察流（重挂：Last-Event-ID / ?after 增量回放
                                  后转 live；无 run → 204）
- POST /sessions/{id}/cancel    : 显式取消当前 run（广播 canceled 终态）

回合与连接解耦（RunManager）：订阅者断开（SPA 导航/刷新/断网）不再终止回合；
流以 done / interrupt / error / canceled 显式收尾。resume 校验图确在中断态，
否则 409 Conflict。
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
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
from app.services.message_content import (
    MessageContentError,
    validate_message_content,
)
from app.services.run_manager import (
    NoSuchRun,
    ResyncNeeded,
    RunConflictError,
    RunLimitError,
)
from app.services.run_source import chat_event_source
from app.streaming.events import SSEEvent

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


async def resolve_agent_choice_or_409(
    st, session: dict, agent_id: uuid.UUID | None = None
) -> uuid.UUID:
    """写者/弱绑定端点共用的智能体解析：显式 agent_id > session.last_agent_id。

    会话-智能体解耦的解析矩阵（plan 1790660700000 §4.4）：
    - 缺省回落 last_agent_id；两者皆无 → 409（空会话未选择 / 上次智能体已删）；
    - **显式无效 ≠ 缺省**：显式传入不存在 / 已归档 / chat 会话选了 cron 型 →
      409，绝不静默回落 last（掩盖选择意图错误比报错更糟）；
    - 显式校验仅对 chat 会话的切换路径生效（cron 会话与其 cron 型 agent 的
      1:1 绑定不受影响，缺省路径保持既有行为：行在即建图，构建失败 → 409）。

    返回选定的 agent_id（不建图）；构图经 _build_agent_or_409。
    """
    explicit = agent_id is not None
    chosen = agent_id or session.get("last_agent_id")
    if chosen is None:
        raise HTTPException(
            status_code=409,
            detail="该会话尚未选择智能体，请先在会话中选择智能体后再操作",
        )
    row = await dbmod.get_agent(st.pool, chosen)
    if row is None:
        if explicit:
            raise HTTPException(status_code=409, detail=f"所选智能体不存在: {chosen}")
        raise HTTPException(
            status_code=409,
            detail="会话上次使用的智能体已被删除，请选择新智能体后重试",
        )
    if explicit and session.get("kind") == "chat":
        if row["status"] != "active":
            raise HTTPException(
                status_code=409, detail=f"智能体「{row['name']}」已归档，不可选择"
            )
        if row["kind"] != "chat":
            raise HTTPException(
                status_code=409,
                detail=f"智能体「{row['name']}」为定时任务专用，不可用于会话",
            )
    return chosen


async def capability_agent_row(
    st, session: dict, agent_id: uuid.UUID | None = None
) -> Optional[dict]:
    """能力端点专用的宽松解析：解析不到 → None（全关降级，200）。

    显式参数优先且无效时不回落 last（与严格解析同规则），但落空不报 409——
    能力谓词是纯展示层输入，悬空 last（agent 已删）/ 未选择 / 选择已失效
    统一降级为"全关"，前端能力驱动 UI 自然隐藏按钮。kind 匹配规则与严格
    解析一致：chat 会话只认 chat 型，cron 会话只认 cron 型（保持任务会话的
    工作区/Shell 徽标行为不变）。
    """
    chosen = agent_id or session.get("last_agent_id")
    if chosen is None:
        return None
    row = await dbmod.get_agent(st.pool, chosen)
    if row is None:
        return None
    if row.get("kind") != session.get("kind", "chat"):
        return None
    return row


async def _refuse_pending_interrupt(agent, config) -> None:
    """HITL 门禁（plan §4.4）：悬空中断存在（state.next 非空）时拒绝新回合。

    行为变更：此前新消息会静默取消待审批（interrupt 被新输入丢弃）；解耦后
    悬空期必须先经 resume（审批）或 withdraw（回撤该回合）收场，再发新消息。
    判据通道级（任意图可读 checkpoint 的 state.next），与图身份无关。
    """
    state = await agent.aget_state(config)
    if state.next:
        raise HTTPException(
            status_code=409,
            detail="该会话存在待审批的中断（HITL），请先处理审批或回撤该回合后再发新消息",
        )


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
    已完成 LLM 调用同样计入。随 run task 执行（与订阅者连接无关）。
    """
    try:
        await dbmod.add_session_usage(pool, session_id, payload)
    except Exception:
        logger.warning("persist usage for session %s failed (skipped)", session_id, exc_info=True)


def _refuse_cron_session(session: dict, *, action: str) -> None:
    """任务会话（kind='cron'）只读守卫：内容由调度器注入，用户不可主动发言/
    追问/回撤/取消（产品决策）。HITL 审批（resume）是唯一放行的交互。"""
    if session.get("kind") == "cron":
        raise HTTPException(
            status_code=409,
            detail=f"任务会话由定时调度驱动，不支持{action}（HITL 审批除外）",
        )


async def _start_run_or_error(st, session: dict, *, kind: str, factory, on_final=None):
    """经 RunManager 启动 run（单写者闸 + usage 即时入库 + 会话 touch 收尾）。"""
    async def _on_usage(payload: dict) -> None:
        await _persist_usage(st.pool, session["id"], payload)

    async def _touch(_final_event) -> None:
        from app.services.session_service import SessionService

        await SessionService(st.pool).touch(session["id"])

    try:
        return await st.run_manager.start_run(
            session["thread_id"],
            session["id"],
            kind,
            factory,
            on_usage=_on_usage,
            on_final=_final_hook(on_final, _touch),
        )
    except RunConflictError as e:
        raise HTTPException(status_code=409, detail=f"该会话已有进行中的回合（{e}）")
    except RunLimitError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _final_hook(extra, touch):
    """合并 on_final 回调（extra 在前、touch 兜底；均 best-effort）。"""
    if extra is None:
        return touch

    async def _hook(final_event) -> None:
        try:
            await extra(final_event)
        except Exception:
            logger.warning("run on_final hook failed (skipped)", exc_info=True)
        await touch(final_event)

    return _hook


async def _subscriber_gen(st, thread_id: str, after: str | None = None):
    """订阅 run → SSE wire dict（每事件带 "{token}.{seq}" id，供重挂对齐）。"""
    try:
        async for event_id, ev in st.run_manager.subscribe(thread_id, after):
            yield ev.to_sse(event_id)
    except NoSuchRun:
        # POST 刚 start 必有 run；GET 已 peek 预检——此处只剩 TTL 过期等竞态，
        # 静默收流（前端按 closed 处理，回落 /history 真值）。
        logger.info("subscriber found no run for thread %s (raced away)", thread_id)
    except ResyncNeeded:
        yield SSEEvent(type="resync", payload={"reason": "buffer-evicted"}).to_sse()


@router.post("/{session_id}/messages")
async def send_message(session_id: uuid.UUID, body: SendMessageRequest, request: Request):
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    _refuse_cron_session(session, action="手动发送消息")
    config = _config_for(session)

    # 写者解析（§4.4）：显式 body.agent_id > last_agent_id，落空/无效 → 409
    chosen_id = await resolve_agent_choice_or_409(st, session, body.agent_id)
    agent = await _build_agent_or_409(st, chosen_id)

    # HITL 门禁（§4.4）：悬空中断存在时拒绝新回合（行为变更：静默取消 → 显式 409）
    await _refuse_pending_interrupt(agent, config)

    # 附件/KB 校验（流外，plan §6.5）—— 与语音 WS 共享同一校验器：能力门控 422 /
    # 附件归属 403 / KB 不存在 404 / 503 以 HTTP 状态码尽早暴露。组装（含图像
    # 预消费与 status 事件）在 run task 内经 chat_event_source 执行。
    try:
        await validate_message_content(
            st,
            session,
            agent_id=chosen_id,
            attachments=body.attachments,
            knowledge_base_ids=body.knowledge_base_ids,
        )
    except MessageContentError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    # last_agent_id 唯一写点（§4.4 写者语义）：校验通过后、开 run 前写入。
    # run 单写者闸排除并发 send；crash 窗口内 last 指向"意图 agent"，无害。
    await dbmod.set_session_last_agent(st.pool, session["id"], chosen_id)

    factory = chat_event_source(
        st,
        session,
        agent,
        config,
        agent_id=chosen_id,
        user_query=body.content,
        attachments=body.attachments,
        knowledge_base_ids=body.knowledge_base_ids,
        rubric=body.rubric,
        raw_content=body.content,
    )
    await _start_run_or_error(st, session, kind="chat", factory=factory)

    # 发起端即首个订阅者：本连接断开只退订，run 由 RunManager 续跑
    return EventSourceResponse(_subscriber_gen(st, session["thread_id"]))


@router.get("/{session_id}/knowledge-capability", response_model=KnowledgeCapability)
async def get_knowledge_capability(
    session_id: uuid.UUID,
    request: Request,
    agent_id: Optional[uuid.UUID] = Query(default=None, description="当前选中智能体；缺省回落 last_agent_id"),
):
    """前端据此渲染会话框 KB 选择器（DESIGN 准则 F2 能力驱动 UI）。

    allow_knowledge = 智能体配置允许 AND 全局 KB 能力已启用（kb_enabled）。
    kb_enabled=false 时直接 false（不查 agent 配置）。解析落空（未选/悬空/
    选择失效）→ 全关降级（capability_agent_row，§4.4）。
    """
    from app.services.session_service import SessionService

    st = get_appstate(request)
    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    allow = False
    if st.knowledge_service is not None:  # kb_enabled
        agent_row = await capability_agent_row(st, session, agent_id)
        if agent_row is not None:
            allow = AgentConfig.model_validate(agent_row["config_json"]).allow_knowledge
    return KnowledgeCapability(allow_knowledge=allow)


@router.get("/{session_id}/shell-capability", response_model=ShellCapability)
async def get_shell_capability(
    session_id: uuid.UUID,
    request: Request,
    agent_id: Optional[uuid.UUID] = Query(default=None, description="当前选中智能体；缺省回落 last_agent_id"),
):
    """是否启用 shell 执行 —— 驱动会话页 shell 能力徽标（准则 F2 能力驱动 UI）。

    allow_shell = 该会话智能体主代理或任一子代理配置 allow_shell=true。
    （compiled 子代理需自身开启才有独立 shell 后端；dicted 子代理共享父
    后端，故随父代理生效。）解析落空 → false（全关降级）。
    """
    st = get_appstate(request)

    from app.services.session_service import SessionService

    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    allow = False
    agent_row = await capability_agent_row(st, session, agent_id)
    if agent_row is not None:
        allow = _allows_shell_anywhere(AgentConfig.model_validate(agent_row["config_json"]))
    return ShellCapability(allow_shell=allow)


def _allows_shell_anywhere(cfg: AgentConfig) -> bool:
    """主代理或任一（递归）子代理声明了 allow_shell 即视为可执行 shell。"""
    if cfg.allow_shell:
        return True
    return any(_allows_shell_anywhere(sa) for sa in (cfg.subagents or []))


@router.get("/{session_id}/interrupt")
async def get_pending_interrupt(
    session_id: uuid.UUID,
    request: Request,
    agent_id: Optional[uuid.UUID] = Query(default=None, description="当前选中智能体；缺省回落 last_agent_id"),
):
    """待审批 HITL 查询（会话重入恢复，plan §5.3 补全）。

    待审批状态在前端仅存活于 live SSE 流内：刷新/切会话即丢失审批卡，
    图却仍暂停在 interrupt（悬空工具卡永远「执行中」）。本端点返回
    checkpointer 真值——与 interrupt SSE 事件同一 payload 形状
    （action_requests + review_configs），前端据此重建 interrupted 状态与
    审批卡。无待审批 → 204。

    弱绑定解析（§4.4）：任一 same-schema 图读到同一 checkpoint state，
    payload 与图身份无关；门禁锁定期间"选中 == last"恒成立，两种解析重合。
    只读 aget_state，不占用 run 闸；resume 端点同源校验（409 契约不变）。
    """
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    config = _config_for(session)
    chosen_id = await resolve_agent_choice_or_409(st, session, agent_id)
    agent = await _build_agent_or_409(st, chosen_id)
    intr = await HITLResolver().assert_interrupted(agent, config)
    # 非字典/空值（如裸 interrupt() 原语）无 action_requests，前端守卫会跳过；
    # 此处一并 204，保持「有返回即可渲染审批卡」的简单契约
    if not intr:
        return Response(status_code=204)
    return intr


@router.post("/{session_id}/resume")
async def resume(session_id: uuid.UUID, body: ResumeRequest, request: Request):
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    config = _config_for(session)
    # 强绑定（§4.4 解析矩阵）：resume 必须跑在恰好那张中断的图上，不收
    # agent_id 参数。门禁 D 保证"存在 pending interrupt ⇒ last_agent_id ==
    # 中断 agent"（悬空期 send 被拒、切换是纯 UI 态不落库），构造性精确。
    chosen_id = await resolve_agent_choice_or_409(st, session)
    agent = await _build_agent_or_409(st, chosen_id)

    resolver = HITLResolver()

    # Contract (plan §11.1): resume MUST verify interrupt state, else 409.
    intr = await resolver.assert_interrupted(agent, config)
    if intr is None:
        raise HTTPException(status_code=409, detail="session is not in an interrupt state")

    decisions = resolver.normalize_decisions(body.decisions)
    is_cron_session = session.get("kind") == "cron"

    on_final = None
    if is_cron_session:
        async def on_final(ev) -> None:
            # 任务会话：审批流的终态同步回任务状态（done→恢复触发、
            # interrupt→继续挂起、error→计失败），不产生 cron_run 行。
            await _sync_cron_job_status(st.pool, session["id"], ev.type)

    def factory():
        return st.stream_adapter.stream_resume(agent, decisions, config)

    await _start_run_or_error(st, session, kind="resume", factory=factory, on_final=on_final)
    return EventSourceResponse(_subscriber_gen(st, session["thread_id"]))


@router.get("/{session_id}/stream")
async def observe_stream(
    session_id: uuid.UUID,
    request: Request,
    after: str | None = None,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    """观察流（只读幂等）：重进会话 / 网络重连的补帧通道。

    - 增量对齐：``?after={lastEventId}``（首挂）或 Last-Event-ID 头（EventSource
      原生重连自动携带）→ 回放缺失事件后转 live；
    - 全量回放：不带 after（刷新后 store 已重建）或 after 属于上一 run（token
      不匹配）→ 从缓冲头回放整个当前回合；
    - 无 run（也无 TTL 窗口内终态）→ 204，前端维持 /history 快照；
    - 落后超出缓冲窗口 → 单发 resync 事件收流，前端回落 /history 重建后重挂。
    """
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    effective_after = after or last_event_id
    if not st.run_manager.peek(session["thread_id"]):
        return Response(status_code=204)
    return EventSourceResponse(
        _subscriber_gen(st, session["thread_id"], effective_after)
    )


@router.post("/{session_id}/cancel")
async def cancel_run(session_id: uuid.UUID, request: Request):
    """显式取消当前 run：广播 canceled 终态（各订阅者据其落态），随后释放
    单写者闸（withdraw 即刻放行）。无 active run → 409。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id)
    _refuse_cron_session(session, action="手动取消")
    if not st.run_manager.cancel(session["thread_id"]):
        raise HTTPException(status_code=409, detail="当前没有进行中的回合")
    return {"ok": True}


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
