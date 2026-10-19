"""Unified MCP client manager (migrated; config-injected instead of global).

Lifecycle enhancements (pooling / health / reconnect) live in
services/mcp_lifecycle.py which wraps MCPClientManager.
"""
from __future__ import annotations

import os

from typing import List

# stdio subprocess transport must not be proxied for localhost MCP servers
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient


class MCPClientManager:
    """Connect to all configured MCP servers in parallel and merge tool lists.

    Thin wrapper over langchain_mcp_adapters MultiServerMCPClient.
    """

    def __init__(self, mcp_config: dict):
        self.config = mcp_config

    async def get_all_mcp_tools(self) -> List[BaseTool]:
        """Parallel-connect all MCP external servers; return merged tool list."""
        if not self.config:
            return []

        client = MultiServerMCPClient(self.config, tool_name_prefix=False)
        try:
            return await client.get_tools()
        except Exception as e:
            # Return the exception object instead of an empty list so callers
            # (MCPLifecycle.connect_one via gather(return_exceptions=True)) can
            # distinguish a real failure from a legitimately empty tool set and
            # surface the cause via _per_server_status["error"].
            return e
