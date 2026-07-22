"""Session CRUD + history (plan §六)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, Request

from app.deps import get_appstate
from app.models.schemas import MessageOut, SessionCreate, SessionOut
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["会话管理"])


def _service(request: Request) -> SessionService:
    return SessionService(get_appstate(request).pool)


def _appstate(request: Request):
    return get_appstate(request)


@router.post("", response_model=SessionOut)
async def create_session(body: SessionCreate, request: Request):
    svc = _service(request)
    try:
        return await svc.create(body.user_id, body.agent_id, body.title, body.thread_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("", response_model=list[SessionOut])
async def list_sessions(user_id: str = Query(...), request: Request = None):
    return await _service(request).list_by_user(user_id)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, request: Request):
    s = await _service(request).get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@router.delete("/{session_id}", response_model=SessionOut)
async def archive_session(session_id: uuid.UUID, request: Request):
    s = await _service(request).archive(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@router.get("/{session_id}/history", response_model=list[MessageOut])
async def get_history(session_id: uuid.UUID, request: Request):
    st = _appstate(request)
    svc = _service(request)
    session = await svc.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    # empty list (not error) when no messages yet (plan §11.1)
    return await svc.get_history(st.checkpointer, session["thread_id"])
