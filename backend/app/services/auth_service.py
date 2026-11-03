"""登录认证服务：登录/会话令牌/注册审批/资料自助修改/管理端操作。

安全基线（与平台"后端是安全"准则一致）：
- 密码仅存 argon2id 哈希，明文永不落库/落日志——管理员亦不可见，只能重置；
- 身份由服务端会话令牌（httpOnly cookie）推导，客户端不再传 user_id；
- 登录失败文案按锚点统一（"用户名或密码错误"/"昵称或密码错误"，防枚举），
  状态化提示仅用于 disabled 等非凭据分支；爆破锁 failed_attempts + locked_until 退避；
- 凭据变更（改密/管理员重置/停用）即时吊销该用户全部会话。
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod

logger = logging.getLogger(__name__)

_hasher = PasswordHasher()
# 用户不存在时的等耗时兜底校验（防凭据路径时序探测）
_DUMMY_HASH = _hasher.hash(secrets.token_hex(32))

PASSWORD_MIN_LEN = 6
USERNAME_MIN_LEN, USERNAME_MAX_LEN = 2, 32
NICKNAME_MIN_LEN, NICKNAME_MAX_LEN = 1, 32
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
# 昵称保留词（防冒充）：casefold 比较
NICKNAME_BLACKLIST = {"admin", "administrator", "root", "system", "goduck", "管理员", "系统管理员", "平台管理员"}
# 登录名/昵称字符集：字母/数字/CJK/下划线/连字符/点/@/空格（首尾空白已 strip）
_NAME_RE = re.compile(r"^[\w\-.@ ]+$", re.UNICODE)


class AuthError(Exception):
    """认证业务错误（router 映射为 HTTPException）。status + 中文提示。"""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _validate_username(username: str) -> str:
    username = (username or "").strip()
    if not (USERNAME_MIN_LEN <= len(username) <= USERNAME_MAX_LEN):
        raise AuthError(400, f"用户名长度须为 {USERNAME_MIN_LEN}-{USERNAME_MAX_LEN} 个字符")
    if not _NAME_RE.match(username):
        raise AuthError(400, "用户名仅允许中英文/数字/下划线/连字符/点/@")
    return username


def _validate_nickname(nickname: str) -> str:
    nickname = (nickname or "").strip()
    if not (NICKNAME_MIN_LEN <= len(nickname) <= NICKNAME_MAX_LEN):
        raise AuthError(400, f"昵称长度须为 {NICKNAME_MIN_LEN}-{NICKNAME_MAX_LEN} 个字符")
    if not _NAME_RE.match(nickname):
        raise AuthError(400, "昵称仅允许中英文/数字/下划线/连字符/点/@")
    if nickname.casefold() in NICKNAME_BLACKLIST:
        raise AuthError(400, "该昵称为系统保留词，请更换")
    return nickname


def _validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LEN:
        raise AuthError(400, f"密码长度不得少于 {PASSWORD_MIN_LEN} 位")
    return password


def _public(user: dict) -> dict:
    """剥离认证内部列（password_hash / 令牌连带列），返回 API 安全视图。"""
    return {k: v for k, v in user.items()
            if k not in ("password_hash", "_token_hash", "_expires_at")}


class AuthService:
    def __init__(self, pool: AsyncConnectionPool, settings=None):
        self.pool = pool
        self.settings = settings
        self.ttl_hours = getattr(settings, "auth_session_ttl_hours", 168) if settings else 168

    # ------------------------------------------------------------------ login
    async def login(self, username: str, password: str, mode: str = "username") -> tuple[dict, str]:
        """校验凭据并签发会话令牌。mode 选锚点（username|nickname）。

        单命名空间解析：一次请求只验一个候选哈希，查无此人不跨域回退。
        """
        identifier = (username or "").strip()
        if mode == "nickname":
            user = await dbmod.get_login_user_by_nickname(self.pool, identifier)
            cred_error = "昵称或密码错误"
        else:
            user = await dbmod.get_user_by_username(self.pool, identifier)
            cred_error = "用户名或密码错误"
        if user is None:
            # 等耗时：用户不存在也走一次完整 argon2 校验
            try:
                _hasher.verify(_DUMMY_HASH, password or "")
            except VerifyMismatchError:
                pass
            raise AuthError(401, cred_error)
        if user.get("locked_until") and user["locked_until"] > datetime.now(timezone.utc):
            remain = int((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1
            raise AuthError(429, f"失败次数过多，账号已锁定，请约 {remain} 分钟后重试")

        ok = False
        try:
            ok = _hasher.verify(user["password_hash"], password or "")
        except VerifyMismatchError:
            ok = False
        except Exception:  # noqa: BLE001 — 哈希格式异常视为校验失败
            logger.exception("password verify error (user=%s)", user["username"])
            ok = False
        if not ok:
            failed = int(user.get("failed_attempts") or 0) + 1
            locked_until = None
            if failed >= MAX_FAILED_ATTEMPTS:
                locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
                failed = 0
                logger.warning("user '%s' locked for %dmin after repeated failures",
                               user["username"], LOCK_MINUTES)
            await dbmod.mark_login_failure(self.pool, user["id"], failed, locked_until)
            raise AuthError(401, cred_error)

        if user["state"] == "disabled":
            raise AuthError(403, "账号已被停用，请联系管理员")
        await dbmod.mark_login_success(self.pool, user["id"])
        token = await self._issue_session(user["id"])
        logger.info("user '%s' logged in via %s (manager=%s)",
                    user["username"], mode, user["is_manager"])
        return _public(user), token

    # ----------------------------------------------------------- session token
    async def _issue_session(self, user_id: uuid.UUID) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = datetime.now(timezone.utc) + timedelta(hours=self.ttl_hours)
        await dbmod.insert_auth_session(self.pool, token_hash, user_id, expires)
        return token

    async def validate_token(self, token: str) -> Optional[dict]:
        """令牌 → 有效用户公开信息；无效/过期/已停用一律 None。"""
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = await dbmod.get_auth_session_user(self.pool, token_hash)
        if user is None or user["state"] != "active":
            return None
        return _public(user)

    async def logout(self, token: str) -> None:
        if token:
            await dbmod.delete_auth_session(
                self.pool, hashlib.sha256(token.encode()).hexdigest()
            )

    async def revoke_user_sessions(self, user_id: uuid.UUID) -> int:
        return await dbmod.delete_user_sessions(self.pool, user_id)

    # --------------------------------------------------------------- register
    async def register(self, username: str, nickname: str, password: str) -> dict:
        """提交注册申请（state=pending，管理员批准后方可登录）。"""
        username = _validate_username(username)
        nickname = _validate_nickname(nickname)
        _validate_password(password)
        if await dbmod.get_user_by_username(self.pool, username) is not None:
            raise AuthError(409, "用户名已存在")
        if await dbmod.get_user_by_nickname(self.pool, nickname) is not None:
            raise AuthError(409, "昵称已被占用")
        for req in await dbmod.find_auth_requests_by_username(self.pool, username):
            if req["status"] == "pending":
                raise AuthError(409, "该用户名的注册申请正在审批中")
            if req["status"] == "rejected":
                raise AuthError(409, "该用户名的注册申请已被拒绝")
        req = await dbmod.insert_auth_request(self.pool, {
            "username": username,
            "nickname": nickname,
            "password_hash": hash_password(password),
        })
        logger.info("register request submitted (username=%s)", username)
        return req

    async def approve_request(self, request_id: uuid.UUID, reviewer_id: uuid.UUID) -> dict:
        try:
            user = await dbmod.approve_register_request(self.pool, request_id, reviewer_id)
        except KeyError as e:
            raise AuthError(404, str(e))
        except FileExistsError:
            raise AuthError(409, "审批冲突：用户名或昵称已被占用，请驳回并让用户重新注册")
        logger.info("register request %s approved", request_id)
        return _public(user)

    async def reject_request(
        self, request_id: uuid.UUID, reviewer_id: uuid.UUID, reason: Optional[str]
    ) -> dict:
        req = await dbmod.reject_register_request(self.pool, request_id, reviewer_id, reason)
        if req is None:
            raise AuthError(404, "申请不存在或已处理")
        return req

    async def delete_request(self, actor: dict, request_id: uuid.UUID) -> None:
        """删除被拒申请记录：释放其用户名占位（拒绝历史一并抹除，仅 rejected 行）。"""
        req = await dbmod.get_auth_request(self.pool, request_id)
        if req is None:
            raise AuthError(404, "申请不存在")
        if req["status"] != "rejected":
            raise AuthError(409, "仅被拒绝的申请可删除记录")
        deleted = await dbmod.delete_rejected_auth_request(self.pool, request_id)
        if deleted is None:
            raise AuthError(409, "申请状态已变更，请刷新后重试")
        logger.info(
            "rejected register request %s (username=%s) deleted by '%s' — username released",
            request_id, (req.get("payload") or {}).get("username"), actor["username"],
        )

    async def list_requests(self, status: Optional[str] = None) -> list[dict]:
        if status is not None and status not in ("pending", "approved", "rejected"):
            raise AuthError(400, "status 仅允许 pending|approved|rejected")
        return await dbmod.list_auth_requests(self.pool, status)

    async def list_users_public(self) -> list[dict]:
        return await dbmod.list_users(self.pool)

    # ---------------------------------------------------------------- profile
    async def change_nickname(self, user: dict, nickname: str) -> dict:
        nickname = _validate_nickname(nickname)
        existing = await dbmod.get_user_by_nickname(self.pool, nickname)
        if existing is not None and existing["id"] != user["id"]:
            raise AuthError(409, "昵称已被占用")
        try:
            updated = await dbmod.update_user_nickname(self.pool, user["id"], nickname)
        except FileExistsError:
            raise AuthError(409, "昵称已被占用")
        if updated is None:
            raise AuthError(404, "用户不存在")
        return _public(updated)

    async def change_password(self, user: dict, old_password: str, new_password: str) -> None:
        """自助改密：验旧密 → 更新 → 吊销全部会话（含当前，前端回登录页）。"""
        _validate_password(new_password)
        full = await dbmod.get_user_by_username(self.pool, user["username"])
        ok = False
        try:
            ok = _hasher.verify(full["password_hash"], old_password or "")
        except VerifyMismatchError:
            ok = False
        if not ok:
            raise AuthError(401, "旧密码错误")
        await dbmod.update_user_password(self.pool, user["id"], hash_password(new_password))
        await self.revoke_user_sessions(user["id"])
        logger.info("user '%s' changed password (sessions revoked)", user["username"])

    # ------------------------------------------------------------------ admin
    async def set_user_state(self, actor: dict, target_id: uuid.UUID, state: str) -> None:
        if state not in ("active", "disabled"):
            raise AuthError(400, "状态仅允许 active|disabled")
        if target_id == actor["id"]:
            raise AuthError(400, "不能对自己执行停用/启用操作")
        target = await dbmod.get_user(self.pool, target_id)
        if target is None:
            raise AuthError(404, "用户不存在")
        await dbmod.set_user_state(self.pool, target_id, state)
        if state == "disabled":
            await self.revoke_user_sessions(target_id)
        logger.info("user '%s' state -> %s (by '%s')", target["username"], state, actor["username"])

    async def admin_reset_password(
        self, actor: dict, target_id: uuid.UUID, new_password: str
    ) -> None:
        """管理员重置密码：不验旧密，重置后吊销目标全部会话（新密码生效）。"""
        _validate_password(new_password)
        target = await dbmod.get_user(self.pool, target_id)
        if target is None:
            raise AuthError(404, "用户不存在")
        await dbmod.update_user_password(self.pool, target_id, hash_password(new_password))
        await self.revoke_user_sessions(target_id)
        logger.info("user '%s' password reset by '%s'", target["username"], actor["username"])

    async def unlock_user(self, target_id: uuid.UUID) -> None:
        target = await dbmod.get_user(self.pool, target_id)
        if target is None:
            raise AuthError(404, "用户不存在")
        await dbmod.unlock_user(self.pool, target_id)

    # -------------------------------------------------------------- bootstrap
    async def bootstrap_users(self) -> None:
        """按 BOOTSTRAP_USERS 建户（create-if-absent）。

        已存在用户以库内为准：昵称/角色漂移仅告警，密码永不覆盖（应用内
        修改优先于 env 初值）。配置格式见 config.bootstrap_users。
        """
        entries = parse_bootstrap_entries(getattr(self.settings, "bootstrap_users", "") or "")
        for entry in entries:
            existing = await dbmod.get_user_by_username(self.pool, entry["username"])
            if existing is not None:
                if existing["nickname"] != entry["nickname"] or \
                        bool(existing["is_manager"]) != entry["is_manager"]:
                    logger.warning(
                        "bootstrap user '%s' exists with different profile "
                        "(db: nickname=%s manager=%s) — keeping db values",
                        entry["username"], existing["nickname"], existing["is_manager"],
                    )
                continue
            await dbmod.insert_user(
                self.pool,
                username=entry["username"],
                nickname=entry["nickname"],
                password_hash=hash_password(entry["password"]),
                is_manager=entry["is_manager"],
            )
            logger.info(
                "bootstrap user '%s' created (nickname=%s manager=%s)",
                entry["username"], entry["nickname"], entry["is_manager"],
            )
        if await dbmod.count_managers(self.pool) == 0:
            logger.warning(
                "no manager account exists — set BOOTSTRAP_USERS (用户名:昵称:manager:密码) "
                "to bootstrap one"
            )


def parse_bootstrap_entries(raw: str) -> list[dict]:
    """解析 BOOTSTRAP_USERS：'用户名:昵称:角色:密码' 逗号分隔多条。

    单条配置非法仅告警跳过（部分可用优于整体不可用）；密码含 ':' 时整段
    留给密码（maxsplit=3）。
    """
    entries: list[dict] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 3)
        if len(parts) != 4 or not all(p.strip() for p in parts[:3]):
            logger.warning("invalid BOOTSTRAP_USERS entry skipped: %r", item)
            continue
        username, nickname, role, password = (p.strip() for p in parts)
        if role not in ("manager", "user"):
            logger.warning("invalid BOOTSTRAP_USERS role '%s' skipped (manager|user)", role)
            continue
        entries.append({
            "username": username, "nickname": nickname,
            "is_manager": role == "manager", "password": password,
        })
    return entries


def parse_legacy_user_map(raw: str) -> dict[str, str]:
    """解析 LEGACY_USER_MAP：'旧值:用户名' 逗号分隔 → {旧值: 用户名}。"""
    mapping: dict[str, str] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        old, sep, username = item.partition(":")
        if sep and old.strip() and username.strip():
            mapping[old.strip()] = username.strip()
        else:
            logger.warning("invalid LEGACY_USER_MAP entry skipped: %r", item)
    return mapping
