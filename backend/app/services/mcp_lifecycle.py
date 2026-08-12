"""MCP connection lifecycle: pool / cache / health-check / reconnect.

Data-driven (plan §5): MCP server registrations live in the `mcp_server` table;
on startup / refresh the lifecycle loads enabled servers, connects each
independently (per-server fault isolation), and exposes tools with namespaced
names (`{server}__{tool}`) to avoid cross-server collisions.

Tool naming: tools are exposed as `{server_name}__{original_tool_name}`.
AgentConfig.tool_names references these namespaced names.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.mcps.mcp_client import MCPClientManager
from app.models import db as dbmod

logger = logging.getLogger(__name__)

_TOOL_SEP = "__"


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(socket.gethostbyname(host)).is_loopback
    except Exception:
        return False


def _validate_http_url(url: str, allow_loopback: bool) -> None:
    """Guard against SSRF for user-supplied http MCP servers."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"MCP http url 非法 scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("MCP http url 缺少 host")
    if not allow_loopback and _is_loopback_host(host):
        raise ValueError(f"MCP http url 指向内网/回环地址被禁止: {host}")


class MCPLifecycle:
    """Manage MCP client connections, driven by the `mcp_server` table."""

    def __init__(self, settings: Settings, pool: AsyncConnectionPool):
        self.settings = settings
        self.pool = pool
        self._tools: list[BaseTool] = []
        self._per_server_tools: dict[str, list[BaseTool]] = {}
        self._per_server_status: dict[str, dict] = {}  # name -> {status, tool_count, kind}
        self._lock = asyncio.Lock()
        self._started = False

    async def startup(self) -> None:
        """Connect all enabled MCP servers, pre-warm tool list."""
        await self.refresh()
        self._started = True
        logger.info(
            "MCPLifecycle started: %d tools across %d servers",
            len(self._tools),
            len(self._per_server_status),
        )

    async def _load_server_rows(self) -> list[dict]:
        rows = await dbmod.list_mcp_servers(self.pool)
        return [r for r in rows if r["enabled"]]

    def _build_sub_config(self, row: dict) -> dict:
        """Build a MultiServerMCPClient sub-config from a mcp_server row.

        Namespaces the server under its registered `name` so tool names get
        prefixed accordingly by the adapter.
        """
        name = row["name"]
        cfg = row["config_json"] or {}
        kind = row["kind"]
        if kind == "stdio":
            return {name: {"transport": "stdio", **cfg}}
        if kind == "sse":
            url = cfg.get("url", "")
            _validate_http_url(url, allow_loopback=self.settings.mcp_allow_loopback)
            return {name: {"transport": "sse", **cfg}}
        if kind in ("http", "streamable_http"):
            # langchain-mcp-adapters SDK 仅识别 "streamable_http"（非 "http"）。
            # 项目内 FastMCP 的 transport="http" 即 streamable HTTP，故两者都映射到它。
            url = cfg.get("url", "")
            _validate_http_url(url, allow_loopback=self.settings.mcp_allow_loopback)
            return {name: {"transport": "streamable_http", **cfg}}
        raise ValueError(f"未知 MCP server kind: {kind}")

    async def refresh(self) -> None:
        """Reload servers from DB and reconnect each (fault-isolated)."""
        async with self._lock:
            rows = await self._load_server_rows()
            tasks = {r["name"]: self._connect_one(r) for r in rows}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            self._per_server_tools.clear()
            self._per_server_status.clear()
            for row, res in zip(rows, results):
                name, kind = row["name"], row["kind"]
                if isinstance(res, Exception):
                    self._per_server_status[name] = {
                        "status": "down",
                        "tool_count": 0,
                        "kind": kind,
                        "error": f"{type(res).__name__}: {res}",
                    }
                    self._per_server_tools[name] = []
                    logger.warning("MCP server '%s' down: %s", name, res)
                else:
                    self._per_server_status[name] = {
                        "status": "ok",
                        "tool_count": len(res),
                        "kind": kind,
                        "error": None,
                    }
                    self._per_server_tools[name] = res
            # flatten with namespaced names; collisions across servers impossible
            # by construction (different server prefixes)
            self._tools = []
            for name, tools in self._per_server_tools.items():
                for t in tools:
                    self._tools.append(self._rename_tool(name, t))

    def _rename_tool(self, server_name: str, tool: BaseTool) -> BaseTool:
        namespaced = f"{server_name}{_TOOL_SEP}{tool.name}"
        if tool.name == namespaced:
            return tool
        # shallow-copy + rename so AgentConfig.tool_names can reference the
        # namespaced identifier uniquely.
        try:
            renamed = tool.model_copy(update={"name": namespaced})
        except Exception:
            tool.name = namespaced
            return tool
        return renamed

    def namespaced_name(self, server_name: str, tool_name: str) -> str:
        return f"{server_name}{_TOOL_SEP}{tool_name}"

    async def _connect_one(self, row: dict) -> list[BaseTool]:
        """Connect a single MCP server independently (fault-isolated)."""
        sub_config = self._build_sub_config(row)
        manager = MCPClientManager(sub_config)
        return await manager.get_all_mcp_tools()

    async def get_tools(self) -> list[BaseTool]:
        """Return cached full (namespaced) tool list."""
        if not self._started:
            await self.startup()
        return self._tools

    async def health_check(self) -> dict:
        """聚合 + 每 server 连接状态，供 /mcp/status 顶部指标与表格状态列。

        返回 {healthy, server_count, tool_count, servers:[{name,healthy,tool_count,kind,error}]}。
        healthy = 无任何 server 处于 down（空集为 True，即未配置 server 视为无异常）。
        """
        servers = [
            {
                "name": name,
                "healthy": s["status"] == "ok",
                "tool_count": s["tool_count"],
                "kind": s["kind"],
                "error": s["error"],
            }
            for name, s in self._per_server_status.items()
        ]
        return {
            "healthy": all(s["healthy"] for s in servers),
            "server_count": len(servers),
            "tool_count": sum(s["tool_count"] for s in servers),
            "servers": servers,
        }

    async def shutdown(self) -> None:
        self._tools = []
        self._per_server_tools = {}
        self._per_server_status = {}
        self._started = False
        logger.info("MCPLifecycle shut down")
