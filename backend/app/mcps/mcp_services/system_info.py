"""System info MCP service (migrated unchanged)."""
from __future__ import annotations

import datetime
import platform

from fastmcp import FastMCP

sys_mcp = FastMCP(name="SystemInfoService", instructions="提供系统信息服务")


@sys_mcp.tool
def get_system_status() -> dict:
    """获取当前服务器系统的核心指标, 如操作系统类型、版本号、当前时间等。

    Returns:
        dict: 系统信息字典
    """
    return {
        "status": "healthy",
        "os_platform": platform.platform(),
        "python_version": platform.python_version(),
        "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
