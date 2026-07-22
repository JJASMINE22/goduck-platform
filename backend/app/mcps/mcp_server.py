"""FastMCP unified gateway (migrated; runs as a separate process over http).

In the modular-monolith early stage this is launched alongside the backend;
future independent deployment only changes transport config (plan §5.4 seam).
"""
from __future__ import annotations

from app.mcps.mcp_services import music_generation_mcp, sys_mcp
from fastmcp import FastMCP

gateway_mcp = FastMCP(
    name="UnifiedGateway",
    instructions="统一网关, 提供系统信息和歌词、音乐生成服务",
)

gateway_mcp.mount(sys_mcp)
gateway_mcp.mount(music_generation_mcp)


def main():
    gateway_mcp.run(transport="http", host="127.0.0.1", port=8088)


if __name__ == "__main__":
    main()
