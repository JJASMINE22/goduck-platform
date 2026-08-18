"""Middleware + backend builders (migrated from DeepAgentsDemos core_utils.py).

Changes:
- No sys.path hack / no base_confs global imports.
- `_build_backend` LOCAL_DIR hardcoded -> config-injected paths.
- `build_rubric_middleware` grader tools built from path-bound factory.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from deepagents import RubricMiddleware
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.store import StoreBackend
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp

from app.core.user_scoped_backend import UserScopedFilesystemBackend
from app.core.user_scoped_shell_backend import UserScopedLocalShellBackend
from app.tools.rubric_tools import make_grader_tools

logger = logging.getLogger(__name__)


# ======================== Middleware ========================

def build_todo_middleware() -> AgentMiddleware:
    """Construct TodoListMiddleware with a version-defensive import.

    TodoListMiddleware 的归属随版本迁移过：langchain>=1.0 位于
    langchain.agents.middleware，早期 deepagents 内置位于 deepagents.middleware。
    deepagents 依赖未 pin 版本，两处按序尝试；均不可用时给出明确修复指引，
    让显式启用 task_planning 的构建在保存期即失败（fail-fast），而非静默降级。
    仅在 task_planning=True 的节点调用（未启用的构建不受依赖版本影响）。
    """
    try:
        from langchain.agents.middleware import TodoListMiddleware  # type: ignore
    except ImportError:  # pragma: no cover - depends on installed versions
        try:
            from deepagents.middleware import TodoListMiddleware  # type: ignore
        except ImportError:
            raise ImportError(
                "task_planning 需要 TodoListMiddleware，但当前 langchain/deepagents "
                "版本均未提供。请升级依赖：pip install -U 'langchain>=1.0' deepagents"
            ) from None
    return TodoListMiddleware()


class ToolErrorHandlingMiddleware(AgentMiddleware):
    """Wrap tool execution: catch exceptions (incl. HITL signals) and convert to
    error ToolMessages for the LLM to reason about.

    NOTE: `except GraphBubbleUp: raise` is CRITICAL — it preserves the HITL
    signal from the interrupt() primitive (fired inside the tools node) so the
    generic `except Exception` below doesn't swallow it. Declarative interrupt_on
    fires in after_model BEFORE the tools node, so it never reaches here.
    """

    @property
    def name(self) -> str:
        return "ToolErrorHandlingMiddleware"

    async def awrap_tool_call(self, request, handler):
        tool_name = request.tool_call.get("name", "unknown")
        tool_args = request.tool_call.get("args", {})
        try:
            return await handler(request)
        except asyncio.TimeoutError:
            return ToolMessage(
                content=f"工具 [{tool_name}] 执行超时。建议：简化请求或稍后重试。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )
        except GraphBubbleUp:
            # Pass through to HITL handler (only for interrupt()-in-tool case).
            raise
        except Exception as e:
            return ToolMessage(
                content=f"工具 [{tool_name}] 执行失败。\n参数: {tool_args}\n错误: {e}\n请根据错误信息调整参数或尝试其他方案。",
                tool_call_id=request.tool_call.get("id", ""),
                status="error",
            )


class FixedRubricMiddleware(RubricMiddleware):
    """Inject a standing rubric when the caller did NOT supply one in payload.

    Priority: invoke payload `rubric` > this middleware's `fixed_rubric`.
    """

    def __init__(self, fixed_rubric: str, model, **kwargs):
        super().__init__(model=model, **kwargs)
        self._fixed_rubric = fixed_rubric

    def _inject(self, state) -> dict | None:
        # Only inject when state has no rubric yet (per-invoke rubric wins).
        if not state.get("rubric", None):
            return {"rubric": self._fixed_rubric}
        return

    def before_agent(self, state, runtime):
        inject = self._inject(state)
        base = super().before_agent(state, runtime)
        # MUST merge rubric into the *returned* state update — otherwise the
        # injection only affects super's internal read but doesn't write to
        # state, so after_agent never sees the rubric.
        if inject and base:
            return {**inject, **base}
        return inject or base

    async def abefore_agent(self, state, runtime):
        inject = self._inject(state)
        base = await super().abefore_agent(state, runtime)
        if inject and base:
            return {**inject, **base}
        return inject or base


# ======================== Rubric middleware build ========================

def _log_evaluation(evaluation: dict) -> None:
    """RubricMiddleware.on_evaluation callback: log each grading round.

    Logging only — does not affect control flow (exceptions swallowed by
    RubricMiddleware itself).
    """
    logger.info(
        "rubric eval | run=%s iter=%s result=%s | %s",
        evaluation.get("grading_run_id"),
        evaluation.get("iteration"),
        evaluation.get("result"),
        (evaluation.get("explanation") or "")[:200],
    )
    for c in evaluation.get("criteria", []):
        flag = "PASS" if c.get("passed") else f"FAIL({c.get('gap', '')[:80]})"
        logger.info("  - %s: %s", c.get("name"), flag)


def build_rubric_middleware(
    rubric_cfg,
    default_model,
    workspace_root: str,
) -> Optional[RubricMiddleware]:
    """Build RubricMiddleware (or FixedRubricMiddleware) from a RubricConfig.

    Args:
        rubric_cfg: schema_utils.RubricConfig instance; None / empty content -> None.
        default_model: grader_model fallback (string or BaseChatModel).
        workspace_root: path for building grader forensics tools.

    Returns:
        RubricMiddleware | None
    """
    if rubric_cfg is None or not (rubric_cfg.content or "").strip():
        return None

    grader_registry = make_grader_tools(workspace_root)
    grader_tools = [
        grader_registry[name]
        for name in (rubric_cfg.grader_tool_names or [])
        if name in grader_registry
    ]

    common = dict(
        model=default_model,
        tools=grader_tools or None,
        max_iterations=rubric_cfg.max_iterations,
        on_evaluation=_log_evaluation,
    )

    if rubric_cfg.fixed:
        return FixedRubricMiddleware(fixed_rubric=rubric_cfg.content, **common)
    return RubricMiddleware(**common)


# ======================== Backend build ========================

def _build_backend(ctx, *, memory, workspace, has_skills, allow_shell):
    """Build CompositeBackend with conditionally-mounted routes + default backend.

    Routes are mounted ONLY when the corresponding config value is truthy
    (config-driven backend assembly, ported from DeepAgentsDemos). All paths
    resolve via the injected BuildContext (no LOCAL_DIR global).

    Args:
        ctx: BuildContext (provides sandbox / store / memories_dir / local_dir /
            skills_dir for path resolution).
        memory: bool (True -> mount /memories/).
        workspace: bool (True -> mount /workspace/ at LOCAL_ROOT/workspace).
        has_skills: bool (True -> mount /skills/).
        allow_shell: bool (True -> default backend is the per-user local shell
            backend, which auto-registers the ``execute`` tool). Route A
            (``ctx.sandbox``) takes precedence if provided; otherwise allow_shell
            chooses shell-vs-StateBackend (the previous default).
    """
    sandbox = ctx.sandbox
    routes = {}
    if memory:
        if ctx.store is not None:
            routes["/memories/"] = StoreBackend(
                store=ctx.store,
                namespace=lambda rt: (rt.server_info.user.identity, "memories"),
            )
        else:
            routes["/memories/"] = FilesystemBackend(
                root_dir=os.path.abspath(ctx.memories_dir).replace("\\", "/"),
                virtual_mode=True,
            )
    if workspace:
        # 按用户隔离：base_root 为全局 workspace 目录，per-user 子目录在每次文件
        # 操作时由 cwd property 按 runtime 当前 user 惰性解析（plan §3.1-1/2）。
        # 绝不能在构建期注入 per-user root——图按 agent_id 缓存（agent_registry.py），
        # 首用户 root 会被烘焙并静默串给所有用户（plan 风险 R3）。
        routes["/workspace/"] = UserScopedFilesystemBackend(
            base_root=ctx.workspace_dir,
            virtual_mode=True,
        )
    if has_skills:
        routes["/skills/"] = FilesystemBackend(
            root_dir=os.path.abspath(ctx.skills_dir).replace("\\", "/"),
            virtual_mode=True,
        )
    # Default backend decides shell capability:
    # - Route A (ctx.sandbox) wins if provided (real BaseSandbox: Docker/VM).
    # - Route B (allow_shell): per-user local shell backend — auto-registers the
    #   `execute` tool (SandboxBackendProtocol). base_root 复用 workspace_dir，
    #   确保 shell cwd == 文件后端 cwd == 浏览 API 根。
    # - otherwise StateBackend (no execute tool) — the previous default.
    if sandbox is not None:
        default_backend = sandbox
    elif allow_shell:
        default_backend = UserScopedLocalShellBackend(base_root=ctx.workspace_dir)
    else:
        default_backend = StateBackend()
    return CompositeBackend(
        default=default_backend,
        routes=routes,
    )
