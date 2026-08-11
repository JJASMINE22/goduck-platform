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
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from deepagents.backends.filesystem import FilesystemBackend
from langgraph.runtime import get_runtime

# Same whitelist as the deepagents StoreBackend namespace validation
# (backends/store.py: _NAMESPACE_COMPONENT_RE): alphanumeric plus characters
# common in user IDs (hyphen, underscore, dot, @, +, colon, tilde). Anything
# else — crucially path separators and traversal sequences — is hashed.
_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9\-_.@+:~]+$")
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


def user_workspace_path(base_root: str | Path, user_id: str | None) -> Path:
    """Resolve the on-disk workspace directory for a user.

    Single source of truth shared by the write backend
    (``UserScopedFilesystemBackend.cwd``) and the browse API
    (``app/api/workspace.py``), guaranteeing "write path == read path"
    (plan §3.1-4 【关键】sanitize 同源). Both sides always derive the user
    subdirectory from the SAME ``settings.workspace_dir`` base and the SAME
    ``_sanitize_user`` mapping, so a backend write under ``u_<hash>/`` can never
    be invisible to the API browsing ``<raw>/`` (or vice versa).
    """
    return Path(base_root).resolve() / _sanitize_user(user_id)


def _current_user_identity() -> str:
    """Read the current user identity from the active LangGraph runtime.

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
    identity = getattr(user, "identity", None) if user is not None else None
    return identity or ""


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
        return self._base_root / _sanitize_user(_current_user_identity())
