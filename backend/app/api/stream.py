"""SSE streaming + HITL resume + run observe/cancel（plan §六 流式对话 + sse-run-detach-reattach）。

四个端点共用同一 SSE 事件协议：
- POST /sessions/{id}/messages : 发消息 → 启动 run → 以订阅者身份返回 SSE
- POST /sessions/{id}/resume    : HITL 决策 → 续跑 run → SSE（同上）
- GET  /sessions/{id}/stream    : 观察流（重挂：Last-Event-ID / ?after 增量回放
                                  后转 live；无 run → 204）
- POST /sessions/{id}/cancel    : 显式取消当前 run（广播 canceled 终态；join
                                   task 终止后清孤儿 pending——中止后可直接再发）

回合与连接解耦（RunManager）：订阅者断开（SPA 导航/刷新/断网）不再终止回合；
流以 done / interrupt / error / canceled 显式收尾。resume 校验图确在中断态，
否则 409 Conflict。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from langgraph.graph import END
from sse_starlette.sse import EventSourceResponse

from app.core.schema_utils import AgentConfig
from app.deps import SimpleUser, get_appstate, get_current_user
from app.models import db as dbmod
from app.models.schemas import (
    KnowledgeCapability,
    QueueOut,
    RequeueRequest,
    ResumeRequest,
    SendMessageRequest,
    ShellCapability,
)
from app.services.hitl_resolver import HITLResolver
from app.services.message_content import (
    MessageContentError,
    validate_message_content,
)
from app.services.queue_service import QueueLimitError
from app.services.run_manager import (
    NoSuchRun,
    ResyncNeeded,
    RunConflictError,
    RunLimitError,
)
from app.services.run_source import chat_event_source
from app.services.stream_adapter import has_interrupt
from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["流式对话"])


def _config_for(session: dict, user: dict) -> dict:
    return {
        "configurable": {
            "thread_id": session["thread_id"],
            # identity=user_id（uuid，memories/会话隔离键不变）；username
            # 仅用于工作区目录键 {username}-{uuid}
            "langgraph_auth_user": SimpleUser(
                str(session["user_id"]), user.get("username")
            ),
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


async def _refuse_or_heal_pending(agent, config) -> None:
    """新回合前置门禁（plan §4.4）+ 中止残留自愈。

    - 干净线程（state.next 为空）：放行；
    - 真 HITL 中断（任一 task.interrupts 非空）：409 拒绝——悬空期必须先经
      resume（审批）或 withdraw（回撤该回合）收场，审批不可被新输入静默丢弃
      （行为变更：静默取消 → 显式 409；判据通道级，与图身份无关）；
    - 孤儿 pending（state.next 非空但无 interrupts：cancelled / 崩溃 run 的
      残留）：以 ``values=None, as_node=END`` 清尾后放行（与 withdraw 清尾
      同手法，见 session_service._apply_withdraw_update——不经 writer 链，
      任何图状态安全）。「中止后直接发新消息」即走此路径，不再强制先撤回。
    """
    state = await agent.aget_state(config)
    if not state.next:
        return
    if has_interrupt(state):
        raise HTTPException(
            status_code=409,
            detail="该会话存在待审批的中断（HITL），请先处理审批或回撤该回合后再发新消息",
        )
    # 中止/崩溃残留：未竟执行就地作废，放行本回合
    await agent.aupdate_state(config, None, as_node=END)


# 取消收尾 join 上限：canceled 广播 + on_final + 生成器链 aclose 通常瞬时完成；
# 超时（事件源卡在不可取消的 I/O 等病态情形）则放弃本次清理，残留交由
# _refuse_or_heal_pending 自愈兜底
_CANCEL_JOIN_TIMEOUT_S = 10.0


async def join_cancelled_run(run, timeout_s: float = _CANCEL_JOIN_TIMEOUT_S) -> bool:
    """等待已请求取消的 run task 真正终止，返回是否在时限内终止。

    取消收尾（clear_orphan_pending）的前置：canceled 广播、on_final 回调、
    事件源生成器链 aclose 全部完成后 checkpoint 写入才停，先行清理会与之竞争。

    - gather(return_exceptions=True)：run task 以 CancelledError / 异常收尾
      对等待方都是"已终止"，不外抛（调用方自身的取消不受影响，正常传播）；
    - shield：超时时 wait_for 只取消外壳，不向已在取消收尾中的 run task
      二次注入 cancel（可能打断其 finally 清理）。
    """
    try:
        await asyncio.wait_for(
            asyncio.shield(asyncio.gather(run.task, return_exceptions=True)),
            timeout=timeout_s,
        )
        return True
    except asyncio.TimeoutError:
        logger.warning(
            "cancelled run %s did not terminate within %.1fs; skip state cleanup",
            run.token,
            timeout_s,
        )
        return False


async def clear_orphan_pending(
    st, session: dict, config: dict, agent_id: uuid.UUID | None = None
) -> bool:
    """清空 cancelled / 崩溃 run 残留的孤儿 pending（取消路径的源头收尾）。

    判据与门禁（_refuse_or_heal_pending）一致：state.next 非空且无任何
    task.interrupts 才清理——真 HITL 中断绝不触碰（审批不可静默丢弃）。
    清尾手法与 withdraw 相同（values=None, as_node=END，不经 writer 链，
    任何状态安全）；取图弱绑定（显式 agent_id > last_agent_id），与回撤同
    规则。返回是否执行了清理；异常向上抛，由调用方决定吞或报（取消路径
    best-effort 吞掉并告警，门禁自愈兜底）。仅 checkpoint 操作，复用调用方
    既有 config（SSE 与语音 WS 同一入口）。
    """
    chosen = await resolve_agent_choice_or_409(st, session, agent_id)
    agent = await _build_agent_or_409(st, chosen)
    state = await agent.aget_state(config)
    if not state.next or has_interrupt(state):
        return False
    await agent.aupdate_state(config, None, as_node=END)
    return True


async def _load_session_or_404(
    request: Request, session_id: uuid.UUID, user: dict | None = None
) -> dict:
    from app.services.session_service import SessionService

    svc = SessionService(get_appstate(request).pool)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if user is not None and session["user_id"] != user["id"]:
        # 非本人会话一律 404（不暴露存在性）
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


async def _start_run_or_error(
    st, session: dict, *, kind: str, factory, on_final=None, queue_hook: bool = True
):
    """经 RunManager 启动 run（单写者闸 + usage 即时入库 + 会话 touch 收尾）。

    queue_hook：终态是否挂队列联动（done 续排 / error 挂起，queued-send §4）。
    排水启动的 run 传 False——行落态与续排由 QueueService 自身的 on_final
    承担，避免双重触发。
    """
    async def _on_usage(payload: dict) -> None:
        await _persist_usage(st.pool, session["id"], payload)

    async def _touch(_final_event) -> None:
        from app.services.session_service import SessionService

        await SessionService(st.pool).touch(session["id"])

    use_queue_hook = queue_hook and st.queue_service is not None

    async def _queue(final_event) -> None:
        await st.queue_service.on_run_final(session["id"], final_event.type)

    try:
        return await st.run_manager.start_run(
            session["thread_id"],
            session["id"],
            kind,
            factory,
            on_usage=_on_usage,
            on_final=_final_hook(on_final, _queue if use_queue_hook else None, _touch),
        )
    except RunConflictError as e:
        raise HTTPException(status_code=409, detail=f"该会话已有进行中的回合（{e}）")
    except RunLimitError as e:
        raise HTTPException(status_code=503, detail=str(e))


def _final_hook(extra, queue, touch):
    """合并 on_final 回调（extra → queue → touch 顺序；均 best-effort）。"""
    hooks = [h for h in (extra, queue, touch) if h is not None]

    async def _hook(final_event) -> None:
        for h in hooks:
            try:
                await h(final_event)
            except Exception:
                logger.warning("run on_final hook failed (skipped)", exc_info=True)

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


async def _enqueue_or_409(st, session: dict, user: dict, chosen_id, body) -> dict:
    """入队分流共用段：上限校验 + 落行 + 解析展示元数据（QueuedMessageOut 形状）。"""
    try:
        item = await st.queue_service.enqueue(
            session,
            user["id"],
            chosen_id,
            body.content,
            rubric=body.rubric,
            attachments=body.attachments,
            knowledge_base_ids=body.knowledge_base_ids,
        )
    except QueueLimitError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return await st.queue_service.to_item_out(item)


@router.post("/{session_id}/messages")
async def send_message(
    session_id: uuid.UUID,
    body: SendMessageRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """发送决策树（queued-send plan §3）：run 活跃 / HITL 悬空 / 队列非空 →
    入队 202；否则直发 SSE 200（既有行为原样）。入队是后端内部分流，前端
    仍只调本接口。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="手动发送消息")
    config = _config_for(session, user)

    # 写者解析（§4.4）+ 内容校验（附件/KB/能力）——两条路径都要，错误尽早暴露
    chosen_id = await resolve_agent_choice_or_409(st, session, body.agent_id)
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

    # ① 回合进行中 → 入队（不建图、不碰门禁；终态后的 on_final 钩子负责续排）
    if st.run_manager.is_active(session["thread_id"]):
        item = await _enqueue_or_409(st, session, user, chosen_id, body)
        return JSONResponse(
            status_code=202, content=jsonable_encoder({"queued": True, "item": item})
        )

    agent = await _build_agent_or_409(st, chosen_id)

    # ② 状态门禁：真 HITL 悬空 → 入队（不清中断，审批仍是唯一推进手段）；
    # 孤儿 pending 自愈（_refuse_or_heal_pending 同手法）后落 ③
    state = await agent.aget_state(config)
    if state.next:
        if has_interrupt(state):
            item = await _enqueue_or_409(st, session, user, chosen_id, body)
            return JSONResponse(
                status_code=202, content=jsonable_encoder({"queued": True, "item": item})
            )
        await agent.aupdate_state(config, None, as_node=END)

    # ③ 队列非空 → 入队尾（核心不变式：下一回合永远是队列头，新消息不插队）
    if await dbmod.count_queued_by_status(st.pool, session_id, ("pending",)):
        item = await _enqueue_or_409(st, session, user, chosen_id, body)
        await st.queue_service.wake_after_enqueue(session_id)
        return JSONResponse(
            status_code=202, content=jsonable_encoder({"queued": True, "item": item})
        )

    # 直发：last_agent_id 唯一写点（§4.4 写者语义）：校验通过后、开 run 前写入。
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
    user: dict = Depends(get_current_user),
):
    """前端据此渲染会话框 KB 选择器（DESIGN 准则 F2 能力驱动 UI）。

    allow_knowledge = 智能体配置允许 AND 全局 KB 能力已启用（kb_enabled）。
    kb_enabled=false 时直接 false（不查 agent 配置）。解析落空（未选/悬空/
    选择失效）→ 全关降级（capability_agent_row，§4.4）。
    """
    from app.services.session_service import SessionService

    st = get_appstate(request)
    session = await SessionService(st.pool).get(session_id)
    if session is None or session["user_id"] != user["id"]:
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
    user: dict = Depends(get_current_user),
):
    """是否启用 shell 执行 —— 驱动会话页 shell 能力徽标（准则 F2 能力驱动 UI）。

    allow_shell = 该会话智能体主代理或任一子代理配置 allow_shell=true。
    （compiled 子代理需自身开启才有独立 shell 后端；dicted 子代理共享父
    后端，故随父代理生效。）解析落空 → false（全关降级）。
    """
    st = get_appstate(request)

    from app.services.session_service import SessionService

    session = await SessionService(st.pool).get(session_id)
    if session is None or session["user_id"] != user["id"]:
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
    user: dict = Depends(get_current_user),
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
    session, _svc = await _load_session_or_404(request, session_id, user)
    config = _config_for(session, user)
    chosen_id = await resolve_agent_choice_or_409(st, session, agent_id)
    agent = await _build_agent_or_409(st, chosen_id)
    intr = await HITLResolver().assert_interrupted(agent, config)
    # 非字典/空值（如裸 interrupt() 原语）无 action_requests，前端守卫会跳过；
    # 此处一并 204，保持「有返回即可渲染审批卡」的简单契约
    if not intr:
        return Response(status_code=204)
    return intr


@router.post("/{session_id}/resume")
async def resume(
    session_id: uuid.UUID,
    body: ResumeRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    config = _config_for(session, user)
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
    user: dict = Depends(get_current_user),
):
    """观察流（只读幂等）：重进会话 / 网络重连的补帧通道。

    - 增量对齐：``?after={lastEventId}``（首挂）或 Last-Event-ID 头（EventSource
      原生重连自动携带）→ 回放缺失事件后转 live；
    - active run 全量补帧：不带 after 且 run 进行中 → 从缓冲头回放整个当前
      回合后转 live；
    - 终态 run 仅对携带本 run 水位的晚归观察者开放回放（补领终态事件）；
      冷进入（无 after）或跨 run 旧水位 → 204，前端维持 /history 快照——
      已完结回合的结果 /history 即真值，回放只会整回合重推一遍（内容重复
      渲染、usage 重复累计，并催化"回放 done → 失效重拉 → 重挂"自激环）；
    - 落后超出缓冲窗口 → 单发 resync 事件收流，前端回落 /history 重建后重挂。
    """
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    effective_after = after or last_event_id
    if not st.run_manager.is_replayable(session["thread_id"], effective_after):
        return Response(status_code=204)
    return EventSourceResponse(
        _subscriber_gen(st, session["thread_id"], effective_after)
    )


@router.post("/{session_id}/cancel")
async def cancel_run(
    session_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    """显式取消（queued-send §5 两级语义，全程持队列锁封闭排水竞态）：

    - 有 active run：杀 task → 广播 canceled 终态（各订阅者据其落态）→ join
      → 清孤儿 pending（既有链路）；join 后（被杀 run 的 on_final 已把 sending
      行置 sent）把全部 pending 批量置 canceled（冻结队列）；
    - 无 active run 但队列非空：仅批量置 canceled，返回 200（不再 409——
      消除"点停止报无回合、片刻后队列开跑"的断裂）；
    - 两者皆无 → 409。

    取消是数据无损操作：content/附件/KB 全保留，复原 = 显式「重新排队」。
    """
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="手动取消")
    async with st.queue_service.lock(session_id):
        run = st.run_manager.cancel(session["thread_id"])
        if run is not None:
            if await join_cancelled_run(run):
                try:
                    await clear_orphan_pending(st, session, _config_for(session, user))
                except Exception:
                    logger.warning(
                        "cancel cleanup failed for session %s (send gate will self-heal)",
                        session_id,
                        exc_info=True,
                    )
            await dbmod.cancel_pending_queued(st.pool, session_id, None)
            return {"ok": True}
        cleared = await dbmod.cancel_pending_queued(st.pool, session_id, None)
        if cleared == 0:
            raise HTTPException(status_code=409, detail="当前没有进行中的回合")
        return {"ok": True, "queue_cleared": cleared}


# ---------------------------------------------------------------------------
# 会话排队发送：队列管理端点（plan queued-send-messages §7）
# ---------------------------------------------------------------------------

@router.get("/{session_id}/queue", response_model=QueueOut)
async def get_queue(
    session_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """队列真值回查（纯读，不触发排水；前端终态后据此决定重挂与气泡物化）。"""
    st = get_appstate(request)
    await _load_session_or_404(request, session_id, user)
    return await st.queue_service.get_queue_view(session_id)


@router.post("/{session_id}/queue/continue", response_model=QueueOut)
async def continue_queue(
    session_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """[继续]：清除暂停（error/withdrawn/restart 均可）并触发排水。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="操作发送队列")
    async with st.queue_service.lock(session_id):
        await dbmod.resume_queue(st.pool, session_id)
    st.queue_service.spawn_drain(session_id)
    return await st.queue_service.get_queue_view(session_id)


@router.post("/{session_id}/queue/clear")
async def clear_queue(
    session_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """[清空]：删除全部非 sent 行并清除暂停（显式放弃在途意图）。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="操作发送队列")
    async with st.queue_service.lock(session_id):
        removed = await dbmod.clear_queued_messages(st.pool, session_id)
        await dbmod.resume_queue(st.pool, session_id)
    return {"ok": True, "removed": removed}


@router.post("/{session_id}/queue/requeue")
async def requeue_queue_items(
    session_id: uuid.UUID,
    request: Request,
    body: RequeueRequest | None = None,
    user: dict = Depends(get_current_user),
):
    """canceled → pending（单条/批量；ids 缺省 = 全部 canceled，created_at 序
    不变）。显式恢复动作：清除暂停并触发排水。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="操作发送队列")
    ids = body.ids if body and body.ids else None
    async with st.queue_service.lock(session_id):
        requeued = await dbmod.requeue_canceled_queued(st.pool, session_id, ids)
        if requeued == 0 and ids:
            raise HTTPException(status_code=404, detail="条目不存在或状态不可复队")
        await dbmod.resume_queue(st.pool, session_id)
    if requeued:
        st.queue_service.spawn_drain(session_id)
    return {"ok": True, "requeued": requeued}


@router.delete("/{session_id}/queue/{item_id}", status_code=204)
async def delete_queue_item(
    session_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """删除单条队列项（pending/canceled/failed 可删；sending = 正在执行的回合，409）。"""
    st = get_appstate(request)
    session, _svc = await _load_session_or_404(request, session_id, user)
    _refuse_cron_session(session, action="操作发送队列")
    row = await dbmod.get_queued_message(st.pool, session_id, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="队列条目不存在")
    if row["status"] == "sending":
        raise HTTPException(status_code=409, detail="该条目正在发送，不可删除")
    await dbmod.delete_queued_message(st.pool, session_id, item_id)
    return None


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
