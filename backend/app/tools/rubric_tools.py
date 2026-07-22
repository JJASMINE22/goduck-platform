"""Rubric grader forensics toolset (migrated; paths config-injected).

These tools mount ONLY onto `RubricMiddleware(tools=...)`, invoked by the grader
sub-agent during evaluation; they never enter main/sub agent toolsets, no HITL.

Design: read-only / idempotent / path-traversal-safe / self-truncating, safe for
grader to call repeatedly.
"""
from __future__ import annotations

import os

from langchain.tools import tool


def make_grader_tools(workspace_root: str) -> dict:
    """Build path-bound grader tools. Called once with settings.workspace_dir."""
    _WORKSPACE = os.path.abspath(workspace_root)

    @tool
    def list_workspace_files(subdir: str = "") -> str:
        """List files under the agent workspace (recursive, up to 50 entries).

        Use this to verify whether deliverables the agent claims to have written
        actually exist on disk.

        Args:
            subdir: optional subdirectory under /workspace, e.g. 'reports'.
        """
        root = os.path.join(_WORKSPACE, subdir) if subdir else _WORKSPACE
        root = os.path.abspath(root)
        if not root.startswith(_WORKSPACE) or not os.path.isdir(root):
            return f"path_not_found: {subdir!r}"
        found = []
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                rel = os.path.relpath(os.path.join(dirpath, fname), _WORKSPACE)
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
        full = os.path.abspath(os.path.join(_WORKSPACE, rel_path))
        if not full.startswith(_WORKSPACE):
            return "rejected=path_traversal"
        if not os.path.isfile(full):
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
