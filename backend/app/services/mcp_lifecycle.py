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
import sys
from pathlib import Path
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.mcps.mcp_client import MCPClientManager
from app.models import db as dbmod

logger = logging.getLogger(__name__)

# stdio stdout 守卫脚本：过滤第三方 server 打到 stdout 的非 JSONRPC 输出
# （见脚本头部说明；office-word-mcp-server 等"吵闹"server 没有它会被判 down）。
STDIO_GUARD_PATH = Path(__file__).resolve().parents[1] / "mcps" / "stdio_guard.py"

# Tool namespace separator: exposed tools are `{server_name}__{tool_name}`.
# Public: api/mcp.py uses it to scan agent configs for references before
# rename/delete (single source of truth for the prefix contract).
TOOL_SEP = "__"


def _format_error(e: BaseException) -> str:
    """单行错误描述（供 /mcp/status error 字段），解包 ExceptionGroup 取真实原因。

    anyio TaskGroup 把 stdio 子进程的真实失败包成 "unhandled errors in a
    TaskGroup (1 sub-exception)"——对排障毫无信息量；递归取 sub-exception
    的类型+消息拼出根因（如 uvx 连不上 pypi 的 TCP error、WinError 267）。
    """
    subs = getattr(e, "exceptions", None)
    if subs:
        inner = "; ".join(_format_error(s) for s in subs[:3])
        out = f"{type(e).__name__}[{inner}]"
    else:
        msg = str(e).strip()
        out = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
    return out[:300]


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
        self._bg_tasks: set[asyncio.Task] = set()

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
            return {name: self._wrap_stdio_with_guard(cfg)}
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

    @staticmethod
    def _wrap_stdio_with_guard(cfg: dict) -> dict:
        """组装 stdio 子配置，默认透明套一层 stdout 守卫进程。

        部分第三方 stdio server（如 office-word-mcp-server）向 stdout print 非
        JSON 内容，mcp 客户端读到第一条垃圾行即 ValidationError → server 判
        down。守卫只做逐行过滤（JSON 对象行原样转发），真实 command（uvx 等）、
        args、env、cwd 均原样生效，可在 config_json 里用 "stdout_guard": false
        对行为良好的 server 显式关闭。

        cwd 改由守卫的 --cwd 参数下发（而非依赖 mcp SDK 版本是否支持 cwd 字段），
        守卫子进程继承本进程环境后再传给真实命令，env 语义不变。
        """
        cfg = dict(cfg)
        if "command" not in cfg:
            raise ValueError("stdio MCP server config 缺少 command")
        # 空 env dict 在部分 mcp SDK 版本会整体替换子进程环境（丢 PATH/SystemRoot），
        # 视同未设置更稳妥
        if cfg.get("env") == {}:
            cfg.pop("env")
        if not cfg.pop("stdout_guard", True):
            return {"transport": "stdio", **cfg}
        cwd = cfg.pop("cwd", None)
        guard_args = [
            str(STDIO_GUARD_PATH),
            *(["--cwd", str(cwd)] if cwd else []),
            "--",
            str(cfg["command"]),
            *[str(a) for a in cfg.get("args", [])],
        ]
        wrapped = {"transport": "stdio", "command": sys.executable, "args": guard_args}
        for key, value in cfg.items():
            if key not in ("command", "args"):
                wrapped[key] = value
        return wrapped

    async def refresh(self) -> None:
        """Reload servers from DB and reconnect each (fault-isolated, time-bounded).

        每个 server 的连接由 mcp_connect_timeout 兜底（asyncio.wait_for）：
        不可达的 stdio/http server（如 uvx 解析包时 pypi 被墙、TCP 黑洞）最多
        烧一个超时窗口即标记 down，不再拖住整轮 refresh。
        """
        async with self._lock:
            rows = await self._load_server_rows()
            tasks = {r["name"]: self._connect_one(r) for r in rows}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            self._per_server_tools.clear()
            self._per_server_status.clear()
            for row, res in zip(rows, results):
                name, kind = row["name"], row["kind"]
                if isinstance(res, BaseException):
                    self._per_server_status[name] = {
                        "status": "down",
                        "tool_count": 0,
                        "kind": kind,
                        "error": _format_error(res),
                    }
                    self._per_server_tools[name] = []
                    logger.warning("MCP server '%s' down: %s", name, _format_error(res))
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

    def refresh_in_background(self) -> None:
        """非阻塞触发 refresh（CRUD 端点用）：保存立即返回，重连在后台进行。

        refresh 内部有 _lock 串行 + 每连接超时，故多次快速保存只会排队做有界
        的幂等重连，不会堆积无限任务。工具/状态由前端轮询（/mcp/status 60s、
        /mcp/tools 30s）收敛，「强制重连」按钮仍走同步 /mcp/refresh。
        """
        task = asyncio.create_task(self.refresh())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _rename_tool(self, server_name: str, tool: BaseTool) -> BaseTool:
        namespaced = f"{server_name}{TOOL_SEP}{tool.name}"
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
        return f"{server_name}{TOOL_SEP}{tool_name}"

    async def _connect_one(self, row: dict) -> list[BaseTool]:
        """Connect a single MCP server independently (fault-isolated, timed)."""
        sub_config = self._build_sub_config(row)
        manager = MCPClientManager(sub_config)
        try:
            return await asyncio.wait_for(
                manager.get_all_mcp_tools(), timeout=self.settings.mcp_connect_timeout
            )
        except asyncio.TimeoutError as e:
            raise TimeoutError(
                f"连接超时（>{self.settings.mcp_connect_timeout}s）：stdio 首次运行需"
                "下载依赖（检查包索引可达性）或 http 端点无响应"
            ) from e

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
        for t in self._bg_tasks:
            t.cancel()
        self._bg_tasks.clear()
        self._tools = []
        self._per_server_tools = {}
        self._per_server_status = {}
        self._started = False
        logger.info("MCPLifecycle shut down")
