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

from app.tools.rubric_tools import make_grader_tools

logger = logging.getLogger(__name__)


# ======================== Middleware ========================

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

def _build_backend(ctx, *, memory, workspace, has_skills):
    """Build CompositeBackend with conditionally-mounted routes.

    Routes are mounted ONLY when the corresponding config value is truthy
    (config-driven backend assembly, ported from DeepAgentsDemos). All paths
    resolve via the injected BuildContext (no LOCAL_DIR global).

    Args:
        ctx: BuildContext (provides sandbox / store / memories_dir / local_dir /
            skills_dir for path resolution).
        memory: bool (True -> mount /memories/).
        workspace: bool (True -> mount /workspace/ at LOCAL_ROOT/workspace).
        has_skills: bool (True -> mount /skills/).
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
        routes["/workspace/"] = FilesystemBackend(
            root_dir=os.path.abspath(os.path.join(ctx.local_dir, "workspace")).replace("\\", "/"),
            virtual_mode=True,
        )
    if has_skills:
        routes["/skills/"] = FilesystemBackend(
            root_dir=os.path.abspath(ctx.skills_dir).replace("\\", "/"),
            virtual_mode=True,
        )
    return CompositeBackend(
        default=sandbox if sandbox is not None else StateBackend(),
        routes=routes,
    )
