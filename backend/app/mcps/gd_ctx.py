"""_gd_ctx 平台级越境身份通道：定义 → 注入 → 服务端接收（单一真相源）。

三时刻线契约（plan: vision-mcp-gdctx-knowledge）：
- 注册期两级门：mcp_server 行 config_json.inject_ctx=true（server 级可信门）∧
  工具 schema 声明 `_gd_ctx` 形参（工具级信号），同时满足才包装——防第三方
  server 撞名收平台身份；
- 构图期 copy-on-wrap：原池对象永不动（`_rename_tool` 同款 model_copy 语义），
  各图持独立 wrapper（闭包只含 agent_id），共享同一条 MCP 连接（逻辑隔离、
  物理不隔离）；
- 推理期零解析：wrapper 仅合成**值**注入 arguments——workspace 越境前取
  contextvar（contextvar 不跨 HTTP 边界）、session_id 取 RunnableConfig 的
  thread_id；模型/知识集裁决全部留在服务端调用时刻现查，绝不快照。

LLM 可见 schema 永不含 `_gd_ctx`：剥离发生在 wrapper 的 args_schema（唯一
隔离点），注入发生在越境前；服务端以同名可选形参接收（FastMCP 3.x 下划线
参数保留在 wire schema，客户端 needs_ctx 据此识别）。
"""
from __future__ import annotations

import inspect
import logging
from typing import Annotated, Any, Optional

from langchain_core.tools import BaseTool, InjectedToolArg
from pydantic import BaseModel, create_model

from app.core.user_scoped_backend import _current_workspace_key

logger = logging.getLogger(__name__)

# 平台级常量（与 mcp_lifecycle.TOOL_SEP 同级约定）：客户端注入键 = 服务端形参名
GD_CTX_KEY = "_gd_ctx"


class GdCtx(BaseModel):
    """越境身份载荷（客户端 wrapper 合成，服务端按声明可选参数接收）。

    workspace 传**键**不传拼好的路径——解析单点留在 user_scoped_backend
    （`user_workspace_path`），两侧 DRY；session_id 为会话 thread_id（稳定、
    唯一、路径安全，作审计位与产物归档前缀）。
    """

    agent_id: str
    workspace: Optional[str] = None
    session_id: Optional[str] = None


def needs_ctx(tool: BaseTool) -> bool:
    """工具级信号：LLM 侧 schema 是否声明了 `_gd_ctx` 形参。

    无参工具的 inputSchema 常缺 `properties`（langchain `tool.args` 访问即
    KeyError），按"未声明"处理——空参工具无从接身份。
    """
    try:
        args = tool.args
    except Exception:  # noqa: BLE001 — schema 无 properties 等形态异常
        return False
    return isinstance(args, dict) and GD_CTX_KEY in args


def _strip_ctx_field(schema: Any) -> Any:
    """schema 去除 `_gd_ctx` 字段（dict 原样浅拷贝，绝不改原对象）。

    adapters 产物为 dict 形态（MCP inputSchema）；pydantic 模型形态走
    create_model 重建，其余形态原样返回（调用方按 needs_ctx 守门，理论上
    不可达，防御性保底）。
    """
    if isinstance(schema, dict):
        props = schema.get("properties")
        if not isinstance(props, dict) or GD_CTX_KEY not in props:
            return schema
        stripped = dict(schema)
        stripped["properties"] = {k: v for k, v in props.items() if k != GD_CTX_KEY}
        required = schema.get("required")
        if isinstance(required, list) and GD_CTX_KEY in required:
            stripped["required"] = [r for r in required if r != GD_CTX_KEY]
        return stripped
    fields = getattr(schema, "model_fields", None)
    if isinstance(fields, dict) and GD_CTX_KEY in fields:
        kept = {k: v for k, v in fields.items() if k != GD_CTX_KEY}
        return create_model(schema.__name__, **kept)
    return schema


def _collect_ctx(agent_id: str) -> GdCtx:
    """合成运行时身份（读上下文失败一律降级为 None，不阻断调用）。"""
    ctx = GdCtx(agent_id=agent_id)
    key = _current_workspace_key()
    if key:
        ctx.workspace = key
    try:
        from langgraph.config import get_config

        cfg = get_config() or {}
        thread_id = (cfg.get("configurable") or {}).get("thread_id")
        if thread_id:
            ctx.session_id = str(thread_id)
    except Exception:  # noqa: BLE001 — 图外调用无 config，留空
        pass
    return ctx


def inject_gd_ctx(tool: BaseTool, agent_id: str) -> BaseTool:
    """copy-on-wrap 包装：剥 schema + 调用时注入身份值。

    - 剥 schema：wrapper 的 args_schema 不含 `_gd_ctx`（LLM 唯一可见面）；
    - 注入：协程被包一层，invoke 时合成 `_gd_ctx` 塞进 arguments 再委派原
      协程（原协程 schema 含该键，MCP 端点校验通过）；
    - runtime 参数：adapters 的 call_tool 声明了 ToolNode 按名注入的
      `runtime`（InjectedToolArg），wrapper 同名声明并原样转发——签名无该
      形参的工具则不传，保持对任意 BaseTool 的包装安全。
    """
    orig_coroutine = getattr(tool, "coroutine", None)
    if orig_coroutine is None:
        raise ValueError(f"工具 {tool.name} 无 coroutine，无法包装 _gd_ctx")
    forwards_runtime = "runtime" in inspect.signature(orig_coroutine).parameters

    async def _wrapped(
        runtime: Annotated[object | None, InjectedToolArg()] = None, **kwargs: Any
    ):
        kwargs[GD_CTX_KEY] = _collect_ctx(agent_id).model_dump()
        if forwards_runtime:
            return await orig_coroutine(runtime=runtime, **kwargs)
        return await orig_coroutine(**kwargs)

    wrapped = tool.model_copy(
        update={"args_schema": _strip_ctx_field(tool.args_schema), "coroutine": _wrapped}
    )
    logger.debug("gd_ctx wrapped tool=%s agent=%s", tool.name, agent_id)
    return wrapped
