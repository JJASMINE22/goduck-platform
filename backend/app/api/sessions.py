"""Session CRUD + history (plan §六)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.stream import (
    _build_agent_or_409,
    _load_session_or_404,
    resolve_agent_choice_or_409,
)
from app.deps import get_appstate, get_current_user
from app.models import db as dbmod
from app.models.schemas import (
    MessageOut,
    SessionCreate,
    SessionOut,
    WithdrawRequest,
    WithdrawResponse,
)
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["会话管理"])


def _service(request: Request) -> SessionService:
    return SessionService(get_appstate(request).pool)


def _appstate(request: Request):
    return get_appstate(request)


def _ensure_owner(session: dict, user: dict) -> None:
    """属主校验：非本人会话一律 404（不暴露存在性）。"""
    if session["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("", response_model=SessionOut)
async def create_session(
    body: SessionCreate, request: Request, user: dict = Depends(get_current_user)
):
    svc = _service(request)
    try:
        return await svc.create(user["id"], body.agent_id, body.title, body.thread_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"会话标题 '{body.title}' 已存在（同用户活跃会话内唯一；归档后可复用）",
        )
    except PermissionError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    include_archived: bool = Query(False),
    request: Request = None,
    user: dict = Depends(get_current_user),
):
    return await _service(request).list_by_user(user["id"], include_archived=include_archived)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    svc = _service(request)
    s = await svc.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_owner(s, user)
    return s


@router.delete("/{session_id}", response_model=SessionOut)
async def archive_session(
    session_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    svc = _service(request)
    s = await svc.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_owner(s, user)
    _refuse_cron_session(s, action="归档")
    archived = await svc.archive(session_id)
    # queued-send：归档即放弃在途意图——pending 批量置 canceled 并清除暂停；
    # purge 路径经 FK CASCADE 连带清队，无需在此处理
    if archived is not None:
        st = _appstate(request)
        if st.queue_service is not None:
            async with st.queue_service.lock(session_id):
                await dbmod.cancel_pending_queued(st.pool, session_id, None)
                await dbmod.resume_queue(st.pool, session_id)
    return archived


@router.post("/{session_id}/purge", status_code=204)
async def purge_session(
    session_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    """Hard delete a session (physical) + checkpoint cleanup. Irreversible."""
    svc = _service(request)
    s = await svc.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_owner(s, user)
    _refuse_cron_session(s, action="彻底删除")
    deleted = await svc.delete(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    return None


def _refuse_cron_session(s: dict, *, action: str) -> None:
    """任务会话的生命周期由任务管理（/cron/jobs）持有：删除任务时其专用会话
    软归档保留产出。此处 409 防止用户侧误删导致任务触发撞 404（产品决策）。"""
    if s.get("kind") == "cron":
        raise HTTPException(
            status_code=409,
            detail=f"任务会话请从定时任务管理删除（{action}被拒绝）",
        )


@router.get("/{session_id}/history", response_model=list[MessageOut])
async def get_history(
    session_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    st = _appstate(request)
    svc = _service(request)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    _ensure_owner(session, user)
    # empty list (not error) when no messages yet (plan §11.1)
    return await svc.get_history(st.checkpointer, session["thread_id"], session["id"])


@router.post("/{session_id}/withdraw", response_model=WithdrawResponse)
async def withdraw_turn(
    body: WithdrawRequest,
    session_id: uuid.UUID,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Withdraw (soft-delete) a full conversation turn anchored on a user message.

    Refuses while a stream is active (409) to avoid racing checkpoint writes;
    the front-end cancels the stream first then retries. The withdrawn user
    text + attachments are returned so the composer can be prefilled for
    edit-and-resend.

    queued-send（plan §5 双闸封闭）：全程持队列锁与排水互斥——锁内复查无
    sending 行（正在排水的条目 = 即将活跃的写者），回撤成功后队列非空则
    挂起（withdrawn，防纠偏句悬空指向已删内容），一次 [继续] 恢复。
    """
    st = _appstate(request)
    session, svc = await _load_session_or_404(request, session_id)
    _ensure_owner(session, user)
    _refuse_cron_session(session, action="回撤消息")

    async with st.queue_service.lock(session_id):
        if await dbmod.count_queued_by_status(st.pool, session_id, ("sending",)):
            raise HTTPException(status_code=409, detail="队列正在发送，请稍候再回撤")

        if st.run_manager.is_active(session["thread_id"]):
            raise HTTPException(status_code=409, detail="流式生成中，请先停止再回撤")

        # 弱绑定取图（§4.4 解析矩阵）：回撤只需任一 same-schema 图（messages/files
        # 基座通道全图同 reducer），显式 agent_id 优先——last 悬空（agent 已删）时
        # "仅选中、不发送"即可恢复回撤。摘要旁通道的清理经锚点归属另行取图。
        chosen_id = await resolve_agent_choice_or_409(st, session, body.agent_id)
        agent = await _build_agent_or_409(st, chosen_id)

        async def _anchor_agent_builder(anchor_id: uuid.UUID):
            """锚点回合归属取图（§4.4）：按 HumanMessage additional_kwargs.agent_id
            构建"产出该回合的图"，供摘要旁通道清理使用。构建失败由调用方降级。"""
            return await _build_agent_or_409(st, anchor_id)

        try:
            result = await svc.withdraw_turn(
                st.checkpointer,
                agent,
                session["thread_id"],
                session["id"],
                body.message_id,
                anchor_agent_builder=_anchor_agent_builder,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        await svc.touch(session["id"])
        await st.queue_service.pause_if_pending(session_id, "withdrawn")
    return result
