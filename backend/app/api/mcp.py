"""MCP ops: server CRUD + connection status + tools list (plan §5.3)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from app.deps import get_appstate
from app.models import db as dbmod
from app.models.schemas import McpServerCreate, McpServerOut, ToolInfo

router = APIRouter(prefix="/mcp", tags=["MCP运维"])


@router.get("/status")
async def mcp_status(request: Request):
    st = get_appstate(request)
    return await st.mcp_lifecycle.health_check()


@router.post("/refresh")
async def mcp_refresh(request: Request):
    """Force reconnect all MCP servers (admin ops)."""
    st = get_appstate(request)
    await st.mcp_lifecycle.refresh()
    return {"ok": True, "status": await st.mcp_lifecycle.health_check()}


@router.get("/servers", response_model=list[McpServerOut])
async def list_servers(request: Request):
    return await dbmod.list_mcp_servers(get_appstate(request).pool)


@router.post("/servers", response_model=McpServerOut)
async def create_server(body: McpServerCreate, request: Request):
    st = get_appstate(request)
    row = await dbmod.upsert_mcp_server(st.pool, body.name, body.kind, body.config, body.enabled)
    await st.mcp_lifecycle.refresh()
    return row


@router.put("/servers/{server_id}", response_model=McpServerOut)
async def update_server(server_id: uuid.UUID, body: McpServerCreate, request: Request):
    st = get_appstate(request)
    existing = [s for s in await dbmod.list_mcp_servers(st.pool) if s["id"] == server_id]
    if not existing:
        raise HTTPException(status_code=404, detail="mcp server not found")
    row = await dbmod.upsert_mcp_server(st.pool, body.name, body.kind, body.config, body.enabled)
    await st.mcp_lifecycle.refresh()
    return row


@router.delete("/servers/{server_id}")
async def delete_server(server_id: uuid.UUID, request: Request):
    st = get_appstate(request)
    ok = await dbmod.delete_mcp_server(st.pool, server_id)
    if not ok:
        raise HTTPException(status_code=404, detail="mcp server not found")
    await st.mcp_lifecycle.refresh()
    return {"ok": True}


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(request: Request):
    """All namespaced MCP tools (for the agent-config multi-select dropdown)."""
    st = get_appstate(request)
    tools = await st.mcp_lifecycle.get_tools()
    return [
        ToolInfo(
            name=t.name,
            description=(t.description or "")[:200],
        )
        for t in tools
    ]
