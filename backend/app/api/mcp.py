"""MCP ops: server CRUD + connection status + tools list (plan §5.3).

Name discipline (aligned with models/agents, mirrors ModelInUseError):
- create: name must not exist（预检 409 + UNIQUE 竞态兜底）。
- update: 按 server_id 更新（不再是按名 upsert）。改名需同时满足：
  (a) 目标名未被其它 server 占用（排除自身）；(b) 无智能体引用旧名的工具
  命名空间 `{old}__{tool}` —— 改名会使所有引用方工具静默失效，故阻断并列出。
- delete: 仍有智能体引用其工具（任意层级 *tool_names 前缀命中）时 409，
  列出引用方供手动解绑（绝不级联改 agent 配置）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import get_appstate, require_manager
from app.models import db as dbmod
from app.models.schemas import McpServerCreate, McpServerOut, McpStatusResponse, ToolInfo
from app.services.mcp_lifecycle import TOOL_SEP

router = APIRouter(prefix="/mcp", tags=["MCP运维"])


def _uses_prefix(node, prefix: str) -> bool:
    """config_json 树中任意层级的 *tool_names 是否引用了该前缀。

    递归扫描 dict/list：tool_names / grader_tool_names / subagents[*].tool_names
    等一切以 tool_names 结尾的列表都按名引用 `{server}__{tool}`，需全覆盖。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k.endswith("tool_names") and isinstance(v, list):
                if any(isinstance(t, str) and t.startswith(prefix) for t in v):
                    return True
            if _uses_prefix(v, prefix):
                return True
    elif isinstance(node, list):
        return any(_uses_prefix(item, prefix) for item in node)
    return False


async def _agents_referencing(st, server_name: str) -> list[dict]:
    """引用该 server 工具命名空间的智能体（id+name），供 409 明细展示。"""
    prefix = f"{server_name}{TOOL_SEP}"
    hits: list[dict] = []
    for a in await dbmod.list_agent_configs(st.pool):
        if _uses_prefix(a["config_json"], prefix):
            hits.append({"id": str(a["id"]), "name": a["name"]})
    return hits


@router.get("/status", response_model=McpStatusResponse)
async def mcp_status(request: Request):
    st = get_appstate(request)
    return await st.mcp_lifecycle.health_check()


@router.post("/refresh")
async def mcp_refresh(request: Request, _admin: dict = Depends(require_manager)):
    """Force reconnect all MCP servers (admin ops)."""
    st = get_appstate(request)
    await st.mcp_lifecycle.refresh()
    return {"ok": True, "status": await st.mcp_lifecycle.health_check()}


@router.get("/servers", response_model=list[McpServerOut])
async def list_servers(request: Request):
    return await dbmod.list_mcp_servers(get_appstate(request).pool)


@router.post("/servers", response_model=McpServerOut)
async def create_server(
    body: McpServerCreate, request: Request, _admin: dict = Depends(require_manager)
):
    st = get_appstate(request)
    # 预检：同名已注册 → 409（不再静默覆盖既有 server 的配置）
    if await dbmod.get_mcp_server_by_name(st.pool, body.name) is not None:
        raise HTTPException(status_code=409, detail=f"MCP server 名 '{body.name}' 已存在")
    try:
        row = await dbmod.insert_mcp_server(
            st.pool, body.name, body.kind, body.config, body.enabled
        )
    except FileExistsError:
        # 并发兜底：预检与 INSERT 之间被抢先注册（UNIQUE 约束兜住）
        raise HTTPException(status_code=409, detail=f"MCP server 名 '{body.name}' 已存在")
    # 后台重连（保存立即返回；状态/工具由前端轮询收敛，同步等待见 /mcp/refresh）
    st.mcp_lifecycle.refresh_in_background()
    return row


@router.put("/servers/{server_id}", response_model=McpServerOut)
async def update_server(
    server_id: uuid.UUID,
    body: McpServerCreate,
    request: Request,
    _admin: dict = Depends(require_manager),
):
    st = get_appstate(request)
    existing = await dbmod.get_mcp_server(st.pool, server_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    # 改名分支：名字是承重的（工具命名空间前缀），两道校验
    if body.name != existing["name"]:
        clash = await dbmod.get_mcp_server_by_name(st.pool, body.name)
        if clash is not None and clash["id"] != server_id:
            raise HTTPException(
                status_code=409, detail=f"MCP server 名 '{body.name}' 已被其它 server 占用"
            )
        referencing = await _agents_referencing(st, existing["name"])
        if referencing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"{len(referencing)} 个智能体的工具配置仍引用 "
                        f"'{existing['name']}{TOOL_SEP}*' 前缀，改名会使其工具全部失效，"
                        "请先调整这些智能体或保持原名"
                    ),
                    "referencing_agents": referencing,
                },
            )
    try:
        row = await dbmod.update_mcp_server(
            st.pool, server_id, body.name, body.kind, body.config, body.enabled
        )
    except FileExistsError:
        # 并发兜底：预检无冲突后、UPDATE 落地前名字被他方占用
        raise HTTPException(
            status_code=409, detail=f"MCP server 名 '{body.name}' 已被其它 server 占用"
        )
    if row is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    # 后台重连（保存立即返回；状态/工具由前端轮询收敛）
    st.mcp_lifecycle.refresh_in_background()
    return row


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: uuid.UUID, request: Request, _admin: dict = Depends(require_manager)
):
    st = get_appstate(request)
    existing = await dbmod.get_mcp_server(st.pool, server_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="mcp server not found")
    # 阻断删除：仍有智能体引用其工具（对齐模型删除的 ModelInUseError 模式）
    referencing = await _agents_referencing(st, existing["name"])
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"{len(referencing)} 个智能体仍在使用该 MCP server 的工具，"
                    "请先在其配置中移除相关工具"
                ),
                "referencing_agents": referencing,
            },
        )
    await dbmod.delete_mcp_server(st.pool, server_id)
    # 后台重连（删除立即返回；状态/工具由前端轮询收敛）
    st.mcp_lifecycle.refresh_in_background()
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
