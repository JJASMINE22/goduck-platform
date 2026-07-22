"""Tools package.

Agent-facing tools are now 100% MCP-driven (registered via the `mcp_server`
table, exposed by MCPLifecycle). This package retains only internal rubric
grader tools, mounted exclusively onto RubricMiddleware (not agent-facing).
"""
from __future__ import annotations

__all__: list[str] = []
