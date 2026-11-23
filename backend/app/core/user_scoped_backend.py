"""User-scoped FilesystemBackend: per-user workspace isolation at invoke time.

Why runtime-scoped (not build-time):
- The agent graph is built ONCE per agent_id and cached (agent_registry.py).
- Injecting a per-user root_dir at build time would bake the first user's root
  into the cached graph and silently reuse it for every subsequent user
  (plan risk R3 — cross-user leakage).
- Instead we keep the shared base root at build time and resolve the per-user
  subdirectory lazily on EVERY file operation via the ``cwd`` property, which
  reads the current LangGraph runtime (contextvar, per asyncio task). This is
  the same mechanism ``/memories/`` uses (StoreBackend namespace lambda in
  core_utils.py) and is concurrency-safe: separate SSE requests are separate
  tasks with separate contextvars, so concurrent users each resolve their own
  directory with no shared mutable state (plan §3.3).

Security:
- ``user_id`` is client-controlled (no auth yet) and is NOT a security boundary
  (soft isolation, single-trust deployment). But path injection via ``user_id``
  ("../") MUST still be blocked. ``_sanitize_user`` whitelists characters (same
  set as the deepagents StoreBackend namespace validation, store.py:128) and
  hashes anything else into a stable opaque directory — the raw ``user_id``
  never becomes a filesystem path component (plan risk R1).
- When called outside graph execution (DEFAULT_RUNTIME has server_info=None) or
  when the runtime carries no user, the identity resolves to "" and the backend
  falls back to a distinct hashed bucket that never matches a real user's
  directory — misuse never reads another user's files.

目录命名（可读性）：目录键为 ``{username}-{user_id}``（见 ``workspace_key``），
仅作用于文件系统；``identity`` 恒为 user_id（uuid），会话/记忆命名空间不受影响。
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from langgraph.runtime import get_runtime

from app.core.file_change_tracker import (
    FileChangeRecord,
    count_line_diff,
    make_diff_patch,
    read_text_if_diffable,
    record_change,
)

logger = logging.getLogger(__name__)

# Whitelist superset of the deepagents StoreBackend namespace validation
# (backends/store.py: _NAMESPACE_COMPONENT_RE): unicode word chars（含 CJK
# 用户名）plus characters common in user IDs (hyphen, underscore, dot, @, +,
# colon, tilde). Word chars exclude path separators, whitespace, and control
# characters — anything else is hashed.
_SAFE_USER_RE = re.compile(r"^[\w\-_.@+:~]+$")
# Names made only of dots resolve to self/parent on the filesystem (".", "..",
# "...") and would escape the per-user root even though each char is whitelisted.
_ALL_DOTS_RE = re.compile(r"^\.+$")


def _sanitize_user(user_id: str | None) -> str:
    """Return a filesystem-safe directory component for a ``user_id``.

    Whitelisted IDs pass through unchanged; any value containing path
    separators, traversal sequences, or other disallowed characters is hashed
    (sha256, first 16 hex chars) into a stable opaque directory name. The raw
    input NEVER becomes a path component, blocking ``../`` escape (plan R1).

    An empty/None ``user_id`` maps to a distinct hashed bucket so out-of-context
    calls never collide with a real user's directory.
    """
    raw = (user_id or "").strip()
    # Whitelist AND reject all-dots names (".", "..", "..." traverse the root).
    if raw and _SAFE_USER_RE.match(raw) and not _ALL_DOTS_RE.match(raw):
        return raw
    return "u_" + hashlib.sha256((raw or "<empty>").encode("utf-8")).hexdigest()[:16]


def workspace_key(user_name: str | None, user_id: str | None) -> str:
    """组合工作区目录键：``{username}-{user_id}``（管理员可读，CJK 放行）。

    username 缺失或含白名单外字符（空格等非 \w 字符，整串会被
    _sanitize_user 哈希反而不可读）时回退纯 user_id —— uuid 恒过白名单，
    键稳定且唯一。所有调用方（SimpleUser / 浏览 API / cron）必须经由本
    函数，禁止手拼。
    """
    uid = (user_id or "").strip()
    name = (user_name or "").strip()
    composed = f"{name}-{uid}"
    if uid and name and _SAFE_USER_RE.match(composed):
        return composed
    return uid


def user_workspace_path(base_root: str | Path, key: str | None) -> Path:
    """Resolve the on-disk workspace directory for a user workspace key.

    Single source of truth shared by the write backend
    (``UserScopedFilesystemBackend.cwd``) and the browse API
    (``app/api/workspace.py``), guaranteeing "write path == read path"
    (plan §3.1-4 【关键】sanitize 同源). ``key`` is the composed workspace key
    (``{username}-{uuid}`` or plain uuid from ``workspace_key``); both sides
    derive it from the SAME inputs so a backend write under one form can never
    be invisible to the API browsing the other.
    """
    return Path(base_root).resolve() / _sanitize_user(key)


def _current_workspace_key() -> str:
    """Read the workspace dir key from the active LangGraph runtime.

    优先 ``user.workspace_key``（``{username}-{uuid}``，SimpleUser 预组合），
    回落 ``user.identity``（uuid）——仅文件系统侧消费；``/memories/`` 命名空间
    直接读 ``identity``，不受本函数影响。

    Returns "" when called outside graph execution (``DEFAULT_RUNTIME`` has
    ``server_info=None``) or when the runtime carries no user — callers then
    fall back to the empty-user hashed bucket, which never matches a real
    user's directory.

    Defensive: isolation lookup must never crash a write/read, so any failure
    reading the runtime degrades to "" rather than raising.
    """
    try:
        rt = get_runtime()
    except Exception:  # noqa: BLE001
        return ""
    server_info = getattr(rt, "server_info", None)
    user = getattr(server_info, "user", None) if server_info is not None else None
    if user is not None:
        key = getattr(user, "workspace_key", None)
        if key:
            return key
        identity = getattr(user, "identity", None)
        if identity:
            return identity
    return ""


class UserScopedFilesystemBackend(FilesystemBackend):
    """FilesystemBackend whose root is resolved per-user on every operation.

    ``cwd`` is a property evaluated on each file operation (read/write/ls/glob),
    so isolation can never go stale and concurrent requests each resolve their
    own user directory — no shared mutable state, no cross-user leakage.

    Note: we deliberately do NOT call ``super().__init__``. The parent
    constructor assigns ``self.cwd = Path(root_dir).resolve()`` as an instance
    attribute, which would conflict with this property (a data descriptor with
    no setter raises ``AttributeError`` on assignment). We instead set the
    attributes the parent would have set (``virtual_mode``,
    ``max_file_size_bytes``) directly and override ``cwd``.
    """

    def __init__(
        self,
        base_root: str | Path,
        *,
        virtual_mode: bool = True,
        max_file_size_mb: int = 10,
    ) -> None:
        self._base_root = Path(base_root).resolve()
        # Mirror parent attributes consumed by FilesystemBackend methods.
        # virtual_mode MUST be True: it enables path-containment checks in
        # _resolve_path (rejects ".." and any resolved path outside the
        # per-user root), which is what makes the per-user jail trustworthy.
        self.virtual_mode = virtual_mode
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024

    @property
    def base_root(self) -> Path:
        """The shared workspace root (parent of the per-user subdirectories)."""
        return self._base_root

    @property
    def cwd(self) -> Path:
        """Per-user working directory, re-resolved on every access.

        Computed from the pre-resolved ``_base_root`` (no extra stat) joined
        with the sanitized current-user identity. We intentionally do NOT
        ``mkdir`` here: reads/ls on a not-yet-created user dir correctly yield
        empty/path_not_found, while ``FilesystemBackend.write`` already
        ``mkdir(parents=True)`` on first write — keeping a read-only property
        free of filesystem side effects.
        """
        return self._base_root / _sanitize_user(_current_workspace_key())


class TrackedUserScopedFilesystemBackend(UserScopedFilesystemBackend):
    """落盘即埋点：write/edit/delete 前后读内容，difflib 计行级差异。

    纯观测（instrumentation），不改任何写入语义：
    - 路径解析、穿越防护、写入/编辑/删除行为 100% 来自 ``super()``；
    - super() 返回 ``error`` 时原样透传、不记录；
    - 记录与否由 collector 决定（contextvar 未激活时 ``record_change`` 静默
      直通），子类本身没有开关分支；
    - 覆写同步方法即同时覆盖异步路径——deepagents 的 ``awrite/aedit/adelete``
      全部是 ``await asyncio.to_thread(self.write/...)``（工具层永远走 async
      变体，to_thread 传播 contextvar，见 file_change_tracker 模块 docstring）。

    统一"前读 → super → 后读 → diff"而非复用 edit 的 old/new_string 参数：
    后者在 ``replace_all``、同回合多次 edit、write 覆写场景下要么信息不全要么
    根本不存在，post-read diff 对所有路径一致且准确。
    """

    def _try_resolve(self, file_path: str) -> Path | None:
        """解析路径；失败（穿越/循环等）返回 None 并完全交由 super() 复现原行为。

        父类 ``write/edit/delete`` 对 ``_resolve_path`` 的 ValueError 不捕获、
        直接向调用方抛出——子类不得吞掉或改变这一行为，故解析失败时零介入。
        """
        try:
            return self._resolve_path(file_path)
        except Exception:  # noqa: BLE001 - 与父类行为对齐（父类稍后同样失败）
            return None

    def write(self, file_path: str, content: str):
        full = self._try_resolve(file_path)
        old_existed = full is not None and full.exists()
        old = read_text_if_diffable(full) if full is not None else None
        result = super().write(file_path, content)
        if result.error:
            return result
        if full is not None:
            new = read_text_if_diffable(full)
            if not old_existed:
                # 新建：旧内容为空，新增行数 = 新文件行数（空文件也记动作本身）
                added, removed = count_line_diff("", new) if new is not None else (None, None)
                diff = make_diff_patch("", new, file_path) if new is not None else None
                record_change(FileChangeRecord(file_path, "create", added, removed, diff))
            elif old is not None and new is not None:
                added, removed = count_line_diff(old, new)
                if added or removed:  # 0/0 视为 touch（等价写回），不记
                    record_change(
                        FileChangeRecord(file_path, "edit", added, removed, make_diff_patch(old, new, file_path))
                    )
            else:
                # 覆写了二进制/超大文件：只记动作，不做行级计数
                record_change(FileChangeRecord(file_path, "edit", None, None))
        return result

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        full = self._try_resolve(file_path)
        before = read_text_if_diffable(full) if full is not None else None
        result = super().edit(file_path, old_string, new_string, replace_all)
        if result.error:
            return result
        if full is not None:
            after = read_text_if_diffable(full)
            if before is not None and after is not None:
                added, removed = count_line_diff(before, after)
                record_change(
                    FileChangeRecord(file_path, "edit", added, removed, make_diff_patch(before, after, file_path))
                )
            else:
                record_change(FileChangeRecord(file_path, "edit", None, None))
        return result

    def delete(self, file_path: str):
        full = self._try_resolve(file_path)
        if full is not None and (full.exists() or full.is_symlink()):
            # 内容在删除后才消失——必须先读先记；目录/二进制不做行级计数
            if full.is_dir():
                record_change(FileChangeRecord(file_path, "delete", None, None))
            else:
                old = read_text_if_diffable(full)
                record_change(FileChangeRecord(
                    file_path, "delete", None,
                    len(old.splitlines()) if old is not None else None,
                    make_diff_patch(old, "", file_path) if old is not None else None,
                ))
        return super().delete(file_path)


def rename_workspace_dirs(users: list[dict], base_root: str | Path) -> int:
    """存量 uuid 目录改可读名 ``{username}-{uuid}``（幂等、best-effort）。

    启动期单线程执行（先于流量）：键回退为 uuid 的用户（含空格等非 \w
    字符的用户名）自动跳过；目标已存在不覆盖（仅告警），仅源目录不存在
    视为已迁移。失败只影响目录可读性，绝不阻断启动。
    """
    root = Path(base_root)
    renamed = 0
    for u in users:
        uid = str(u["id"])
        key = workspace_key(u.get("username"), uid)
        if key == uid:
            continue
        old, new = root / uid, root / key
        if not old.exists():
            continue
        if new.exists():
            logger.warning("workspace dir rename skipped (target exists): %s", new)
            continue
        try:
            old.rename(new)
            renamed += 1
        except OSError:
            logger.warning("workspace dir rename failed (best-effort): %s", old, exc_info=True)
    if renamed:
        logger.info("renamed %d workspace dir(s) to readable names", renamed)
    return renamed
