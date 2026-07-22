"""Agent graph factories (migrated from DeepAgentsDemos graph_utils.py).

Changes:
- No sys.path hack / no base_confs global imports.
- model / checkpointer / store / paths injected via BuildContext (dependency
  injection), factory signatures otherwise preserved.

Two creation paths both supported (plan handover):
- create_agent_recursive : nested CompiledSubAgent topology (+ dicted hybrid)
- create_agent_dictionary : dict-based subagent config (is_dicted flag chooses)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from deepagents import CompiledSubAgent, create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.graph.state import CompiledStateGraph

from app.core.core_utils import _build_backend, build_rubric_middleware
from app.core.schema_utils import AgentConfig


@dataclass
class BuildContext:
    """Dependency-injected build context (replaces base_confs globals)."""

    model: object
    checkpointer: object
    store: Optional[object] = None
    sandbox: Optional[object] = None
    local_dir: str = ""
    memories_dir: str = ""
    workspace_dir: str = ""
    skills_dir: str = ""


def _has_skills_anywhere(agent_config: AgentConfig) -> bool:
    """递归判断主代理或其任意子代理是否声明了 skills。

    skills 的文件系统后端由主代理统一构建、子代理继承, 因此只要树中任一节点
    声明了 skills, 就需要在主代理 backend 上挂载 /skills/ 路由。
    """
    if agent_config.skills:
        return True
    for sa in (agent_config.subagents or []):
        if _has_skills_anywhere(sa):
            return True
    return False


def create_agent_recursive(
    agent_config: AgentConfig,
    tools: List = None,
    middleware: Optional[List[AgentMiddleware]] = None,
    ctx: BuildContext = None,
) -> CompiledStateGraph:
    """Recursively build subagents and assemble agent (compiled + dicted hybrid).

    Constraints:
        * dicted subagents cannot nest other subagents (leaf only)
        * tool-related middleware cannot be configured on dicted path (breaks HITL)
    """
    assert isinstance(agent_config, AgentConfig)
    assert ctx is not None, "BuildContext (ctx) is required"
    tools = tools or []
    _cp = ctx.checkpointer

    # read config-driven backend triggers BEFORE to_dict() (from typed object)
    cfg_memory = agent_config.memory
    cfg_workspace = agent_config.workspace
    cfg_has_skills = _has_skills_anywhere(agent_config)

    # rubric middleware comes from config, independent of agent tools; read from
    # the strongly-typed object BEFORE to_dict().
    rubric_middleware = build_rubric_middleware(
        agent_config.rubric, ctx.model, workspace_root=ctx.workspace_dir
    )

    agent_config = agent_config.to_dict()
    subagents = []
    if agent_config.get("subagents", []):
        for subagent_config in agent_config["subagents"]:
            subagent_config_obj = AgentConfig.model_validate(subagent_config)
            subagent = create_agent_recursive(
                subagent_config_obj, tools, middleware, ctx=ctx
            )
            subagents.append(subagent)

    if agent_config.get("is_dicted", False):
        # dicted subagent: tool-related middleware stays bound to tools;
        # rubric middleware is tool-independent, injected separately (no HITL, safe).
        base_middleware = (
            middleware if agent_config.get("tool_names", []) and middleware else ()
        )
        agentic_middleware = (
            [*base_middleware, rubric_middleware] if rubric_middleware else base_middleware
        )

        agent_config.update(
            {"tools": [t for t in tools if t.name in agent_config.get("tool_names", [])]}
        )
        agent_config.update({"middleware": agentic_middleware})
        agent_config.pop("is_dicted")
        agent_config.pop("tool_names", None)
        return agent_config

    is_main_agent = agent_config.get("is_main", False)
    memory = cfg_memory if is_main_agent else None
    backend = (
        _build_backend(
            ctx,
            memory=memory,
            workspace=cfg_workspace,
            has_skills=cfg_has_skills,
        )
        if is_main_agent
        else None
    )

    agent_tools = [t for t in tools if t.name in agent_config.get("tool_names", [])]
    agent_skills = agent_config.get("skills", None)
    interrupt_on = agent_config.get("interrupt_on", None)

    # Middleware mounting strategy:
    # - tool-related middleware (e.g. error handling) only when tools exist (preserves HITL)
    # - rubric middleware independent of tools; mounts whenever config declares it
    tool_middleware = middleware if agent_tools and middleware else ()
    agentic_middleware = (
        [*tool_middleware, rubric_middleware] if rubric_middleware else tool_middleware
    )

    create_kwargs = dict(
        model=ctx.model,
        tools=agent_tools,
        system_prompt=agent_config["system_prompt"],
        middleware=agentic_middleware,
        subagents=subagents,
        skills=agent_skills,
        memory=memory,
        interrupt_on=interrupt_on,
        backend=backend,
        checkpointer=_cp if is_main_agent else True,
    )
    if ctx.store is not None:
        create_kwargs["store"] = ctx.store

    agent = create_deep_agent(**create_kwargs)

    if not is_main_agent:
        agent = CompiledSubAgent(
            name=agent_config["name"],
            description=agent_config["description"],
            runnable=agent,
        )
    return agent


def create_agent_dictionary(
    agent_config: AgentConfig,
    tools: List = None,
    middleware: Optional[List[AgentMiddleware]] = None,
    ctx: BuildContext = None,
) -> CompiledStateGraph:
    """Walk subagent dicts and assemble agent.

    Constraints:
        * dicted subagents cannot nest other subagents (leaf only)
        * tool-related middleware cannot be configured on dicted path (breaks HITL)
    """
    assert isinstance(agent_config, AgentConfig)
    assert ctx is not None, "BuildContext (ctx) is required"
    tools = tools or []
    _cp = ctx.checkpointer

    # read config-driven backend triggers BEFORE to_dict()
    cfg_memory = agent_config.memory
    cfg_workspace = agent_config.workspace
    cfg_has_skills = _has_skills_anywhere(agent_config)

    # main agent rubric (from config), decoupled from tools
    main_rubric_middleware = build_rubric_middleware(
        agent_config.rubric, ctx.model, workspace_root=ctx.workspace_dir
    )

    agent_config = agent_config.to_dict()
    subagents_config = agent_config.pop("subagents", [])
    for subagent_config in subagents_config:
        subagent_config.update(
            {"tools": [t for t in tools if t.name in subagent_config.get("tool_names", [])]}
        )
        base_middleware = (
            middleware if subagent_config.get("tools", []) and middleware else ()
        )
        # subagent rubric (from its own config)
        sub_rubric_cfg = subagent_config.get("rubric")
        sub_rubric_middleware = None
        if sub_rubric_cfg:
            from app.core.schema_utils import RubricConfig as _RC

            sub_rubric_middleware = build_rubric_middleware(
                _RC.model_validate(sub_rubric_cfg),
                ctx.model,
                workspace_root=ctx.workspace_dir,
            )
        sub_middleware = (
            [*base_middleware, sub_rubric_middleware]
            if sub_rubric_middleware
            else base_middleware
        )
        subagent_config.update({"middleware": sub_middleware})
        subagent_config.pop("tool_names", None)
        subagent_config.pop("is_dicted", None)
        subagent_config.pop("rubric", None)

    memory = cfg_memory
    backend = _build_backend(
        ctx,
        memory=memory,
        workspace=cfg_workspace,
        has_skills=cfg_has_skills,
    )

    main_agent_tools = [t for t in tools if t.name in agent_config.get("tool_names", [])]
    main_agent_skills = agent_config.get("skills", None)
    main_agent_interrupt_on = agent_config.get("interrupt_on", None)

    tool_middleware = middleware if main_agent_tools and middleware else ()
    main_middleware = (
        [*tool_middleware, main_rubric_middleware]
        if main_rubric_middleware
        else tool_middleware
    )

    create_kwargs = dict(
        model=ctx.model,
        tools=main_agent_tools,
        system_prompt=agent_config["system_prompt"],
        middleware=main_middleware,
        subagents=subagents_config,
        skills=main_agent_skills,
        memory=memory,
        interrupt_on=main_agent_interrupt_on,
        backend=backend,
        checkpointer=_cp,
    )
    if ctx.store is not None:
        create_kwargs["store"] = ctx.store

    return create_deep_agent(**create_kwargs)
