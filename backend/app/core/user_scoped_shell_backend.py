"""User-scoped LocalShellBackend: per-user shell execution at invoke time.

Inherits ``LocalShellBackend`` and only overrides ``cwd`` so the shell's working
directory (and all inherited filesystem tools) resolve to the current user's
subdirectory — lazily, per operation, via the LangGraph runtime contextvar
(concurrency-safe; same mechanism as ``UserScopedFilesystemBackend``).

How the cwd override coexists with the parent constructor:
- ``FilesystemBackend.__init__`` assigns ``self.cwd = Path(root_dir).resolve()``
  as a plain instance attribute. We expose ``cwd`` as a property; a get-only
  property would make that assignment raise ``AttributeError``. So we add a
  **no-op setter**: the parent's static assignment is harmlessly discarded, and
  every read returns the per-user directory. Everything else (``execute``,
  ``id``, output handling, timeouts) is inherited unchanged from
  ``LocalShellBackend``.

Two small overrides remain necessary:
- ``execute()`` ensures the per-user directory exists before the host shell runs
  in it (``FilesystemBackend.write`` lazily mkdirs on first write, but
  ``execute`` may run first).
- the inherited env is replaced with a **secret-scrubbed** one so platform
  credentials (``DATABASE_URL``/``MINIO_*_KEY``/``OPENAI_API_KEY``/...) are never
  handed to an LLM-controlled subprocess.

!!! warning "This is NOT a security boundary (Route B)"
    ``cwd`` only sets the shell's **starting** directory; it is NOT a jail. A
    command can ``cd /``, use absolute paths, or read ``../../`` to reach any
    path on the host — including other users' workspaces. Once ``execute`` is
    exposed, the filesystem path-containment of the file tools is moot for an
    agent that can shell out. Use this only in single-trust deployments, always
    behind the auto-injected ``interrupt_on={"execute": True}`` HITL gate.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from deepagents.backends.local_shell import DEFAULT_EXECUTE_TIMEOUT, LocalShellBackend

from app.core.user_scoped_backend import (
    _current_workspace_key,
    user_workspace_path,
)

DEFAULT_MAX_OUTPUT_BYTES = 100_000
"""Default captured-output cap in bytes (mirrors LocalShellBackend)."""

# Minimal, non-secret system variables required for command resolution. Platform
# credentials live in bespoke names (DATABASE_URL, *_KEY, *_PASSWORD, ...) which
# are never present here, so the subprocess cannot read them via `env`.
_EXEC_ENV_WHITELIST = frozenset(
    {
        # POSIX
        "PATH", "SHELL", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR",
        # Windows (cmd.exe / command resolution)
        "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
    }
)

# Belt-and-suspenders: scrub any name that looks like a secret even if it ever
# enters the whitelist. Matches the credential names used across this project.
_SECRET_ENV_RE = re.compile(r"(?i)(KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL|DSN|_URL|AUTH)")


def _build_safe_env() -> dict[str, str]:
    """Whitelisted, secret-scrubbed environment for the shell subprocess."""
    return {
        k: v
        for k, v in os.environ.items()
        if k in _EXEC_ENV_WHITELIST and not _SECRET_ENV_RE.search(k)
    }


class UserScopedLocalShellBackend(LocalShellBackend):
    """``LocalShellBackend`` whose cwd resolves per-user on every operation.

    ``root_dir`` and the shell tuning params are accepted (via ``**kwargs``) but
    default to sane hardcoded values; the static ``root_dir`` is vestigial
    because ``cwd`` is overridden per-user. ``execute()``/``id`` are inherited;
    only ``cwd`` (per-user) and ``execute`` (mkdir guard) are overridden.
    """

    def __init__(self, base_root: str | Path, **kwargs) -> None:
        self._base_root = Path(base_root).resolve()
        super().__init__(
            root_dir=kwargs.pop("root_dir", str(self._base_root)),
            virtual_mode=kwargs.pop("virtual_mode", True),
            timeout=kwargs.pop("timeout", DEFAULT_EXECUTE_TIMEOUT),
            max_output_bytes=kwargs.pop("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES),
            env=kwargs.pop("env", _build_safe_env()),
            inherit_env=kwargs.pop("inherit_env", False),
        )

    @property
    def base_root(self) -> Path:
        """Shared workspace root (parent of the per-user subdirectories)."""
        return self._base_root

    @property
    def cwd(self) -> Path:
        """Per-user working directory, re-resolved on every access.

        Same derivation as ``UserScopedFilesystemBackend.cwd`` so shell cwd ==
        file-tool cwd == browse API root (``user_workspace_path``): a file the
        agent writes with ``write_file`` and one it creates via ``echo > ...``
        land in the identical on-disk directory (写=读).
        """
        return user_workspace_path(self._base_root, _current_workspace_key())

    @cwd.setter
    def cwd(self, _value: Path) -> None:
        # FilesystemBackend.__init__ assigns ``self.cwd`` from ``root_dir``; we
        # resolve per-user lazily, so discard the parent's static assignment
        # (without this no-op setter, that assignment raises AttributeError).
        pass

    def execute(self, command: str, *, timeout: int | None = None):
        """Run a shell command in the per-user cwd (inherited logic + mkdir guard).

        ``FilesystemBackend.write`` lazily ``mkdir(parents=True)`` on first write,
        but ``execute`` may run before any write, so ensure the directory exists
        first; everything else is delegated to ``LocalShellBackend.execute``
        (output capture, truncation, exit code, timeout handling).
        """
        try:
            self.cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            # If mkdir fails, let the parent's subprocess surface the real error.
            pass
        return super().execute(command, timeout=timeout)
