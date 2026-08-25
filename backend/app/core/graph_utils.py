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

from app.core.core_utils import _build_backend, build_rubric_middleware, build_todo_middleware
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


def _main_delegation_guard(has_subagents: bool, workspace: bool) -> str | None:
    """主代理委派纪律（构建期追加到 system_prompt，仅主代理 + 有子代理时生效）。

    子代理不继承主代理上下文，只接收 task 的 description 与主代理落盘的文件。故主代理
    解析出文档/图像/知识库内容后委派时，必须传递【完整解析结果】——精确任务（如专利审查）
    依赖完整原文，仅凭摘要/摘录委派会失真。workspace=True 时落盘 /workspace/ 传路径（无损）；
    否则退化为 description 直传（有损，且受长度限制）。
    """
    if not has_subagents:
        return None
    head = (
        "\n\n---\n\n## 委派纪律（平台自动注入）\n"
        "你下挂了子代理。子代理不继承你的上下文，只接收 task 的 description 与你落盘的文件。"
        "当你已通过工具解析出文档/图像/知识库内容、并需要委派子代理处理时：\n"
        "- 必须传递【完整解析结果】，禁止仅凭摘要或摘录委派——"
        "精确任务（如专利审查）依赖完整原文，任何省略都会导致失真。\n"
    )
    if workspace:
        return head + (
            "- 把完整解析结果写入 /workspace/（如 /workspace/parsed_<id>.md），"
            "再在 task 的 description 中传入该文件路径与任务说明。\n"
        )
    return head + (
        "- 在 task 的 description 中尽量完整转述子代理所需内容。注意：当前未启用工作区，"
        "大块内容（整篇文档/图像描述）受 description 长度限制难以无损传递，"
        "建议启用工作区以 /workspace/ 文件中转。\n"
    )


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
    cfg_allow_shell = agent_config.allow_shell
    cfg_task_planning = agent_config.task_planning
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
        agentic_middleware: list = (
            [*base_middleware, rubric_middleware] if rubric_middleware else [*base_middleware]
        )
        # task planning 与 rubric 同类：工具无关（write_todos 是自注册工具，不包裹
        # 既有工具调用），dicted 路径可安全注入；不继承主代理，按本节点开关生效。
        if cfg_task_planning:
            agentic_middleware.append(build_todo_middleware())

        agent_config.update(
            {"tools": [t for t in tools if t.name in agent_config.get("tool_names", [])]}
        )
        agent_config.update({"middleware": agentic_middleware})
        agent_config.pop("is_dicted")
        agent_config.pop("tool_names", None)
        return agent_config

    is_main_agent = agent_config.get("is_main", False)
    memory = cfg_memory if is_main_agent else None
    # 主代理始终构建后端；子代理显式声明 workspace 时也为其构建独立后端。其 /workspace/
    # 路由与主代理指向同一磁盘目录 (LOCAL_ROOT/workspace)，故子代理写入即落盘、主代理及
    # 其余带 workspace 的子代理皆可读取。memory 仍由上面的 is_main_agent 守住，子代理
    # 不会挂载 /memories/ 路由（偏好记忆仍为主代理独占）。
    needs_backend = is_main_agent or cfg_workspace or cfg_allow_shell
    backend = (
        _build_backend(
            ctx,
            memory=memory,
            workspace=cfg_workspace,
            has_skills=cfg_has_skills,
            allow_shell=cfg_allow_shell,
        )
        if needs_backend
        else None
    )

    agent_tools = [t for t in tools if t.name in agent_config.get("tool_names", [])]
    agent_skills = agent_config.get("skills", None)
    interrupt_on = agent_config.get("interrupt_on", None)
    # 强制 execute HITL：allow_shell 一开，每条 shell 命令必须人工审批（Route B
    # 唯一靠谱的护栏）。execute 由 middleware 自动注册、不在 tool_names，故前端
    # interrupt UI 无法勾选——必须在此自动注入。
    if cfg_allow_shell:
        interrupt_on = {**(interrupt_on or {}), "execute": True}

    # Middleware mounting strategy:
    # - tool-related middleware (e.g. error handling) only when tools exist (preserves HITL)
    # - rubric middleware independent of tools; mounts whenever config declares it
    # - todo middleware (task planning) likewise tool-independent; per-node opt-in
    tool_middleware = middleware if agent_tools and middleware else ()
    agentic_middleware = (
        [*tool_middleware, rubric_middleware] if rubric_middleware else [*tool_middleware]
    )
    if cfg_task_planning:
        agentic_middleware.append(build_todo_middleware())
    if agent_config.get("dynamic_dispatch", False):
        # lazy import: langchain_quickjs 仅在启用动态子代理时需要,
        # 避免对不使用 dynamic_dispatch 的部署强制该依赖 (需 `pip install -e ".[quickjs]"`)。
        from langchain_quickjs import CodeInterpreterMiddleware

        agentic_middleware = [*agentic_middleware, CodeInterpreterMiddleware()]

    memory_sources = ["/memories/preferences.md"] if memory else None

    if is_main_agent:
        _guard = _main_delegation_guard(bool(subagents), cfg_workspace)
        if _guard:
            agent_config["system_prompt"] += _guard

    create_kwargs = dict(
        model=ctx.model,
        tools=agent_tools,
        system_prompt=agent_config["system_prompt"],
        middleware=agentic_middleware,
        subagents=subagents,
        skills=agent_skills,
        memory=memory_sources,
        interrupt_on=interrupt_on,
        backend=backend,
        checkpointer=_cp # if is_main_agent else True,
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
    cfg_allow_shell = agent_config.allow_shell
    cfg_task_planning = agent_config.task_planning
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
        sub_middleware: list = (
            [*base_middleware, sub_rubric_middleware]
            if sub_rubric_middleware
            else [*base_middleware]
        )
        # 任务规划：per-node 开关，子代理声明式 spec 的 middleware 字段生效（不继承主代理）
        if subagent_config.get("task_planning", False):
            sub_middleware.append(build_todo_middleware())
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
        allow_shell=cfg_allow_shell,
    )

    main_agent_tools = [t for t in tools if t.name in agent_config.get("tool_names", [])]
    main_agent_skills = agent_config.get("skills", None)
    main_agent_interrupt_on = agent_config.get("interrupt_on", None)
    # 强制 execute HITL（同 create_agent_recursive；Route B 唯一靠谱护栏）。
    
    if cfg_allow_shell:
        main_agent_interrupt_on = {**(main_agent_interrupt_on or {}), "execute": True}

    tool_middleware = middleware if main_agent_tools and middleware else ()
    main_middleware: list = (
        [*tool_middleware, main_rubric_middleware]
        if main_rubric_middleware
        else [*tool_middleware]
    )
    if cfg_task_planning:
        main_middleware.append(build_todo_middleware())

    memory_sources = ["/memories/preferences.md"] if memory else None

    _guard = _main_delegation_guard(bool(subagents_config), cfg_workspace)
    if _guard:
        agent_config["system_prompt"] += _guard

    create_kwargs = dict(
        model=ctx.model,
        tools=main_agent_tools,
        system_prompt=agent_config["system_prompt"],
        middleware=main_middleware,
        subagents=subagents_config,
        skills=main_agent_skills,
        memory=memory_sources,
        interrupt_on=main_agent_interrupt_on,
        backend=backend,
        checkpointer=_cp,
    )
    if ctx.store is not None:
        create_kwargs["store"] = ctx.store

    return create_deep_agent(**create_kwargs)
