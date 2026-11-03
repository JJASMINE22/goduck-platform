"""Rubric grader forensics toolset (migrated; paths config-injected).

These tools mount ONLY onto `RubricMiddleware(tools=...)`, invoked by the grader
sub-agent during evaluation; they never enter main/sub agent toolsets, no HITL.

Design: read-only / idempotent / path-traversal-safe / self-truncating, safe for
grader to call repeatedly.

Per-user scoping (plan risk R4): the grader runs inside the agent graph (same
pregel task / contextvar as the main agent), so it resolves the SAME per-user
workspace directory the main agent writes to — otherwise self-evaluation would
read another user's files. The user dir is derived via the shared
``user_workspace_path`` helper (identical sanitize mapping to the write backend),
guaranteeing "写在哪=读在哪".
"""
from __future__ import annotations

import os
from pathlib import Path

from langchain.tools import tool

from app.core.user_scoped_backend import _current_workspace_key, user_workspace_path


def make_grader_tools(workspace_root: str) -> dict:
    """Build path-bound grader tools.

    ``workspace_root`` is the shared base (``settings.workspace_dir``); the
    per-user subdirectory is resolved at call time from the active runtime so a
    cached graph never bakes in one user's root.
    """
    _BASE = Path(workspace_root).resolve()

    def _user_root() -> Path:
        # Resolved per-call from the current graph runtime. Outside graph
        # execution the key is "" → hashed bucket → reads nothing useful,
        # but never another user's files.
        return user_workspace_path(_BASE, _current_workspace_key())

    def _safe_join(subdir: str) -> Path | None:
        """Join subdir onto the per-user root with a containment guard."""
        root = _user_root()
        rel = (subdir or "").strip().lstrip("/")
        candidate = (root / rel).resolve() if rel else root
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    @tool
    def list_workspace_files(subdir: str = "") -> str:
        """List files under the agent workspace (recursive, up to 50 entries).

        Use this to verify whether deliverables the agent claims to have written
        actually exist on disk.

        Args:
            subdir: optional subdirectory under /workspace, e.g. 'reports'.
        """
        root = _safe_join(subdir)
        if root is None or not root.is_dir():
            return f"path_not_found: {subdir!r}"
        found = []
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                rel = os.path.relpath(os.path.join(dirpath, fname), root)
                found.append(rel.replace("\\", "/"))
                if len(found) >= 50:
                    break
            if len(found) >= 50:
                break
        return f"count={len(found)}; files={found}"

    @tool
    def read_workspace_file(rel_path: str, max_chars: int = 4000) -> str:
        """Read a text file under the agent workspace and return its content.

        Args:
            rel_path: path relative to /workspace, e.g. 'reports/summary.md'.
            max_chars: max characters to return (default 4000).
        """
        full = _safe_join(rel_path)
        if full is None:
            return "rejected=path_traversal"
        if not full.is_file():
            return f"not_found: {rel_path!r}"
        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read(max_chars + 1)
        if len(text) > max_chars:
            text = text[:max_chars] + "...(truncated)"
        return text

    return {
        "list_workspace_files": list_workspace_files,
        "read_workspace_file": read_workspace_file,
    }
