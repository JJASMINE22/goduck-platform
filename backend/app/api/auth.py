"""登录认证路由：登录/登出/注册/资料自助 + 管理端（审批/用户管理）。

会话凭证 = httpOnly Cookie（goduck_session）承载的服务端会话令牌：同源
fetch/EventSource(SSE)/WebSocket 自动携带，语音 WS 无需自定义头；登出/
停用/改密 = 删会话行即时吊销。SameSite=Lax 兜底 CSRF（跨站 POST 不带 cookie）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.deps import SESSION_COOKIE, get_appstate, get_current_user, require_manager
from app.models.schemas import (
    AdminPasswordReset,
    AdminUserOut,
    AuthRequestOut,
    LoginRequest,
    NicknameUpdate,
    PasswordChange,
    RegisterRequest,
    RejectRequest,
    UserOut,
    UserStateUpdate,
)
from app.services.auth_service import AuthError, AuthService

router = APIRouter(prefix="/auth", tags=["登录认证"])


def _service(request: Request) -> AuthService:
    st = get_appstate(request)
    return AuthService(st.pool, st.settings)


def _http_error(e: AuthError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


def _cookie_kwargs(request: Request, service: AuthService) -> dict:
    """cookie 属性：https（直连或经反代 X-Forwarded-Proto）下加 Secure。"""
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return {
        "key": SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": scheme == "https",
        "path": "/",
        "max_age": service.ttl_hours * 3600,
    }


@router.post("/login", response_model=UserOut)
async def login(body: LoginRequest, request: Request):
    svc = _service(request)
    try:
        user, token = await svc.login(body.username, body.password, body.mode)
    except AuthError as e:
        raise _http_error(e)
    resp = JSONResponse(UserOut(**user).model_dump(mode="json"))
    resp.set_cookie(value=token, **_cookie_kwargs(request, svc))
    return resp


@router.post("/logout")
async def logout(request: Request, user: dict = Depends(get_current_user)):
    await _service(request).logout(request.cookies.get(SESSION_COOKIE) or "")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return user


@router.post("/register", status_code=202)
async def register(body: RegisterRequest, request: Request):
    """提交注册申请（pending），管理员批准后方可登录。"""
    try:
        await _service(request).register(body.username, body.nickname, body.password)
    except AuthError as e:
        raise _http_error(e)
    return {"ok": True, "message": "注册申请已提交，请等待管理员审批"}


@router.put("/profile", response_model=UserOut)
async def update_profile(
    body: NicknameUpdate, request: Request, user: dict = Depends(get_current_user)
):
    """改昵称：即时查重 + 保留词黑名单，生效零审批（用户名不可改）。"""
    try:
        return await _service(request).change_nickname(user, body.nickname)
    except AuthError as e:
        raise _http_error(e)


@router.post("/password")
async def change_password(
    body: PasswordChange, request: Request, user: dict = Depends(get_current_user)
):
    """自助改密（验旧密 + 两次新密确认）。成功即吊销全部会话——前端跳登录页。"""
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        await _service(request).change_password(user, body.old_password, body.new_password)
    except AuthError as e:
        raise _http_error(e)
    resp = JSONResponse({"ok": True, "message": "密码已修改，请重新登录"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ============================ 管理端（require_manager）======================


def _request_out(row: dict) -> AuthRequestOut:
    p = row.get("payload") or {}
    return AuthRequestOut(
        id=row["id"], status=row["status"], reason=row.get("reason"),
        username=p.get("username", ""), nickname=p.get("nickname", ""),
        created_at=row["created_at"], reviewed_at=row.get("reviewed_at"),
    )


@router.get("/requests", response_model=list[AuthRequestOut])
async def list_requests(
    request: Request,
    status: Optional[str] = Query(default=None, description="pending|approved|rejected；缺省全部"),
    _admin: dict = Depends(require_manager),
):
    rows = await _service(request).list_requests(status)
    return [_request_out(r) for r in rows]


@router.post("/requests/{request_id}/approve", response_model=UserOut)
async def approve_request(
    request_id: uuid.UUID, request: Request, _admin: dict = Depends(require_manager)
):
    try:
        user = await _service(request).approve_request(request_id, _admin["id"])
    except AuthError as e:
        raise _http_error(e)
    return user


@router.post("/requests/{request_id}/reject", response_model=AuthRequestOut)
async def reject_request(
    request_id: uuid.UUID,
    body: RejectRequest,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    try:
        row = await _service(request).reject_request(request_id, _admin["id"], body.reason)
    except AuthError as e:
        raise _http_error(e)
    return _request_out(row)


@router.delete("/requests/{request_id}")
async def delete_request(
    request_id: uuid.UUID, request: Request, admin: dict = Depends(require_manager)
):
    """删除被拒申请记录：释放其用户名占位（仅 rejected 行可删）。"""
    try:
        await _service(request).delete_request(admin, request_id)
    except AuthError as e:
        raise _http_error(e)
    return {"ok": True}


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(request: Request, _admin: dict = Depends(require_manager)):
    return await _service(request).list_users_public()


@router.post("/users/{user_id}/state")
async def set_user_state(
    user_id: uuid.UUID,
    body: UserStateUpdate,
    request: Request,
    admin: dict = Depends(require_manager),
):
    try:
        await _service(request).set_user_state(admin, user_id, body.state)
    except AuthError as e:
        raise _http_error(e)
    return {"ok": True}


@router.post("/users/{user_id}/unlock")
async def unlock_user(
    user_id: uuid.UUID, request: Request, _admin: dict = Depends(require_manager)
):
    try:
        await _service(request).unlock_user(user_id)
    except AuthError as e:
        raise _http_error(e)
    return {"ok": True}


@router.post("/users/{user_id}/password")
async def admin_reset_password(
    user_id: uuid.UUID,
    body: AdminPasswordReset,
    request: Request,
    admin: dict = Depends(require_manager),
):
    """管理员重置密码（哈希不可逆，原密码不可见；重置后目标全部会话吊销）。"""
    if body.new_password != body.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        await _service(request).admin_reset_password(admin, user_id, body.new_password)
    except AuthError as e:
        raise _http_error(e)
    return {"ok": True}
