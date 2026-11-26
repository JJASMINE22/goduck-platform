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

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from deepagents import CompiledSubAgent, create_deep_agent
from langchain.agents.middleware.types import AgentMiddleware
from langgraph.graph.state import CompiledStateGraph

from app.core.core_utils import _build_backend, build_rubric_middleware, build_todo_middleware
from app.core.schema_utils import AgentConfig, _tree_interrupt_labels

logger = logging.getLogger(__name__)


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
    # Jev shell 守卫工厂（构建期注入，None=能力未启用/未开守卫）。
    # 每次调用产出独立的 (when, description) 闭包对（含独立缓存），供本节点
    # interrupt_on["execute"] 使用；阈值随闭包烘焙（TTL 内改配置不生效，plan §三）。
    jev_gate_factory: Optional[Callable[[], tuple[Callable, Callable]]] = None
    # Jev rubric 门控工厂（构建期注入，None=能力未启用）。
    # 签名 factory(rubric_cfg) -> RubricJudge：节点级 questions/thresholds 在
    # 消费点（build_rubric_middleware）经 rubric_cfg 渲染，平台级参数
    # （client/审计/全局阈值）随工厂闭包烘焙——两级烘焙，混合树天然支持
    # （llm 子代理与 jev 子代理并存）。plan: jev-rubric-grader-gate。
    jev_rubric_factory: Optional[Callable] = None
    # Jev KB 检索门控工厂（构建期注入，None=能力未启用）。
    # 签名 factory() -> JevKbRerankMiddleware：kb 门控无节点级参数（问题集为
    # 代码常量），消费点（主代理构建）直接实例化；mode/阈值随工厂闭包烘焙
    # （TTL 语义同上）。plan: jev-kb-rerank-middleware。
    jev_kb_factory: Optional[Callable[[], AgentMiddleware]] = None
    # 知识集卡片（agent 级 kb_ids → {id,name,description}，构建期现查）：
    # 知识检索纪律提示注入的唯一前置（空卡片 = 未绑定，不注入）。
    kb_cards: Optional[List[dict]] = None


def _strip_cron_unattended(root: AgentConfig) -> None:
    """usage=cron 构建期兜底剥离（validator 为第一道门，此处覆盖直写 DB 等旁路）。

    就地清空全树 interrupt_on、allow_knowledge 与 kb_ids：无人值守运行期不
    触发 HITL 中断（interrupt 事件在 cron 下无消费方），不注入 KB 检索纪律
    提示（固定 prompt 无知识集语义，注入反而诱导编造 kb_ids）。剥离发生时
    打 warning（fail-visible）。allow_attachments 不参与图构建，无需剥离。
    """
    labels = _tree_interrupt_labels(root)
    if root.allow_knowledge:
        labels.append("allow_knowledge")
    if root.kb_ids:
        labels.append("kb_ids")
    if not labels:
        return

    def walk(node: AgentConfig) -> None:
        node.interrupt_on = None
        node.allow_knowledge = False
        node.kb_ids = []
        for sa in node.subagents or []:
            walk(sa)

    walk(root)
    logger.warning(
        "usage=cron agent: stripped unattended-forbidden capabilities at build (%s); "
        "fix the agent config to silence this",
        "; ".join(labels),
    )


def resolve_execute_interrupt(
    cfg_allow_shell: bool,
    cfg_shell_guard: bool,
    jev_gate_factory: Optional[Callable[[], tuple[Callable, Callable]]],
) -> Optional[dict]:
    """计算 execute 的 interrupt_on 覆盖项（纯函数，便于单测）。

    Returns:
        None: 未开启 allow_shell（不注入，execute 键不存在）。
        {"execute": True}: 原行为兜底（守卫未开 / Jev 能力未启用）。
        {"execute": {allowed_decisions/when/description}}: 守卫生效——
        值升级为 dict 形态，键永存（无条目=默认自动放行，execute 键不可删）。
        allowed_decisions 收窄为三态（去掉 respond：shell 审批无"代答"语义）。
    """
    if not cfg_allow_shell:
        return None
    if cfg_shell_guard and jev_gate_factory is not None:
        when_fn, desc_fn = jev_gate_factory()
        return {
            "execute": {
                "allowed_decisions": ["approve", "edit", "reject"],
                "when": when_fn,
                "description": desc_fn,
            }
        }
    return {"execute": True}


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


def _shell_workspace_note(allow_shell: bool, workspace: bool) -> str | None:
    """shell 虚实路径对齐提示（构建期追加 system_prompt，per-node）。

    动机：文件工具走 CompositeBackend 虚拟路由（/workspace/ 等前缀映射到按
    用户隔离的磁盘目录），execute 却在真实宿主 shell 中执行——shell 的 cwd
    恰为同一目录，但虚拟前缀本身在宿主上并不存在。模型若在 shell 中使用字面
    /workspace/...（如 ls /workspace、cat /workspace/x.md）会得到 not found，
    误判"文件丢失"而重复落盘，甚至以绝对路径在工作区之外制造孤儿文件
    （Windows 本地开发下 /workspace 解析到盘符根，可能真的建出目录）。

    纯认知对齐：不改任何执行语义；未开 allow_shell 的节点不注入。
    """
    if not allow_shell:
        return None
    head = (
        "\n\n---\n\n## Shell 与文件工具的路径对齐（平台自动注入）\n"
        "你同时持有文件工具与 execute（shell），二者路径体系不同：\n"
        "- 文件工具（ls/read/write/edit/glob/grep）使用虚拟路径，虚拟根即你的专属工作区；\n"
        "- execute 在真实 shell 中执行，其当前工作目录（pwd）就是文件工具虚拟根的落点，"
        "两者指向同一批文件。\n"
    )
    if workspace:
        mapping = (
            "- /workspace/ 是虚拟挂载：文件工具写 /workspace/report.md，在 shell 中对应"
            "当前工作目录下的 report.md。**禁止**在 shell 中使用字面 /workspace/... 绝对"
            "路径（宿主上不存在该目录，必然 not found）；一律改用相对路径"
            "（ls、cat report.md、find . -name 'report.md'）。\n"
        )
    else:
        mapping = (
            "- shell 中访问文件工具写过的文件时，一律使用相对路径；不要把文件工具的"
            "虚拟绝对路径（/xxx）直接交给 shell 执行。\n"
        )
    tail = (
        "- 验证落盘是否成功优先用文件工具，而不是在 shell 中按绝对路径查找。\n"
        "- 其余虚拟挂载（/skills/、/memories/ 等，如已启用）同样仅文件工具可见，"
        "shell 中不可达。\n"
    )
    return head + mapping + tail


def _kb_capability_prompt(
    tool_names: List[str], kb_cards: Optional[List[dict]] = None
) -> str | None:
    """知识集检索纪律（构建期追加到 system_prompt，kb v2：agent 级知识集）。

    前置：tool_names 持有 {server}__kb_search ∧ 知识集非空（kb_cards，kb_ids
    现查解析的卡片）才注入——空集注入点名反而诱导编造。提示面列出绑定知识
    库（名称/kb_id/描述），白话提示驱动 LLM 按相关性自主触发；是否检索、
    检索几次由 agent 自主判断（提示已明示无关问题可直接作答——强制检索
    回环门已退役：回答结束后重复提醒/强迫无关检索，观感差，见
    _maybe_append_jev_kb_gate 注释）。
    """
    kb_tool = next((n for n in tool_names if str(n).endswith("__kb_search")), None)
    if not kb_tool or not kb_cards:
        return None
    rows = "\n".join(
        f"| {i + 1} | {c['name']} | {c['id']} | {(c.get('description') or '（无描述）')[:60]} |"
        for i, c in enumerate(kb_cards)
    )
    return (
        "\n\n---\n\n## 知识库检索纪律（平台自动注入）\n"
        f"你持有知识库检索工具 `{kb_tool}`，本智能体绑定了以下知识库：\n\n"
        "| 序号 | 知识库 | kb_id | 描述 |\n|------|--------|-------|------|\n"
        f"{rows}\n\n"
        "回答与上述知识库内容相关的问题时：\n"
        f"- 必须先调用 `{kb_tool}` 检索，kb_ids 参数只使用上表真实 kb_id，禁止编造"
        "（与问题相关的多个角度可分次检索）；**此优先级高于本提示中任何意图路由"
        "规则**（如按意图选择电商/网络搜索工具的规则一律让位）\n"
        "- 答案优先依据检索结果作答并标注来源；检索未命中或无相关内容时，"
        "如实说明后再按自身能力作答，不得编造检索结果\n"
        "- 与绑定知识库明显无关的问题可直接回答，无需检索\n"
    )


def _maybe_append_jev_kb_gate(
    agentic_middleware: list,
    ctx: BuildContext,
    cfg_allow_knowledge: bool,
    tool_names: List[str],
) -> None:
    """jev kb 重排门挂载（主代理独占，v2）。

    检索触发由系统提示的知识集纪律驱动、agent 自主决定；本门只做检索结果的
    相关性降噪（高置信无关才滤，JEV_KB_RERANK_ENABLED，默认关），置于列表
    末尾=最内侧（first defined = outermost，ToolErrorHandlingMiddleware 在
    最外层兜底）。前置：allow_knowledge ∧ 持有 {server}__kb_search ∧ 知识集
    非空（ctx.kb_cards）。

    退役注记：KbPrerequisiteMiddleware（无检索证据 → 注入提醒回环强制检索）
    已删除——终答后最多两轮系统提醒会迫使 agent 对无关问题强行 kb_search
    并整段重述回答（实测 9 库绑定的机器视觉 agent 被迫检索 GLM-5.4 问题），
    提示面已含"无关免检"纪律，由 agent 自主决策即可。
    """
    if not cfg_allow_knowledge:
        return
    kb_tool = next((n for n in (tool_names or []) if str(n).endswith("__kb_search")), None)
    if kb_tool is None or not getattr(ctx, "kb_cards", None):
        return
    factory = getattr(ctx, "jev_kb_factory", None)
    if factory is not None:
        kb_mw = factory()
        agentic_middleware.append(kb_mw)
        logger.info("jev kb gate attached (mode=%s)", getattr(kb_mw, "kb_mode", "unknown"))


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
    # usage=cron 兜底剥离（仅根进入：递归调用 is_main=False 自然跳过）
    if agent_config.is_main and agent_config.usage == "cron":
        _strip_cron_unattended(agent_config)
    tools = tools or []
    _cp = ctx.checkpointer

    # read config-driven backend triggers BEFORE to_dict() (from typed object)
    cfg_memory = agent_config.memory
    cfg_workspace = agent_config.workspace
    cfg_allow_shell = agent_config.allow_shell
    cfg_task_planning = agent_config.task_planning
    cfg_allow_knowledge = getattr(agent_config, "allow_knowledge", False)
    cfg_shell_guard = getattr(agent_config, "shell_guard", False)
    cfg_has_skills = _has_skills_anywhere(agent_config)

    # rubric middleware comes from config, independent of agent tools; read from
    # the strongly-typed object BEFORE to_dict().
    rubric_middleware = build_rubric_middleware(
        agent_config.rubric, ctx.model, workspace_root=ctx.workspace_dir, ctx=ctx
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
    # 强制 execute HITL：allow_shell 一开，每条 shell 命令必须过审（Route B
    # 唯一靠谱的护栏）。execute 由 middleware 自动注册、不在 tool_names，故前端
    # interrupt UI 无法勾选——必须在此自动注入。shell_guard 开启且 Jev 能力可用时
    # 值升级为 dict 形态（when 门控），否则保持 True（现状兜底）。语义：值升级，
    # 键永存（interrupt_on 无条目=默认自动放行，execute 键不可删）。
    overlay = resolve_execute_interrupt(cfg_allow_shell, cfg_shell_guard, ctx.jev_gate_factory)
    if overlay is not None:
        interrupt_on = {**(interrupt_on or {}), **overlay}

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
        # KB 检索纪律：allow_knowledge 仅主代理可开，故只在此注入（与委派纪律同点）
        if cfg_allow_knowledge:
            _kb = _kb_capability_prompt(
                agent_config.get("tool_names", []), getattr(ctx, "kb_cards", None)
            )
            if _kb:
                agent_config["system_prompt"] += _kb
            # jev kb 重排门挂载与纪律提示同点位同条件（allow_knowledge 主代理独占）
            _maybe_append_jev_kb_gate(
                agentic_middleware, ctx, cfg_allow_knowledge,
                agent_config.get("tool_names", []),
            )

    # shell 虚实路径对齐提示（per-node）：置于 is_main_agent 块外，独立开启
    # allow_shell 的 compiled 子代理同样获得提示（递归使其 per-node 生效）；
    # dicted 叶子已提前返回，共享父 backend，由父节点的提示覆盖。
    _shell_note = _shell_workspace_note(cfg_allow_shell, cfg_workspace)
    if _shell_note:
        agent_config["system_prompt"] += _shell_note

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
        checkpointer=_cp,
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
    # usage=cron 兜底剥离（本函数只以根节点进入）
    if agent_config.is_main and agent_config.usage == "cron":
        _strip_cron_unattended(agent_config)
    tools = tools or []
    _cp = ctx.checkpointer

    # read config-driven backend triggers BEFORE to_dict()
    cfg_memory = agent_config.memory
    cfg_workspace = agent_config.workspace
    cfg_allow_shell = agent_config.allow_shell
    cfg_task_planning = agent_config.task_planning
    cfg_allow_knowledge = getattr(agent_config, "allow_knowledge", False)
    cfg_shell_guard = getattr(agent_config, "shell_guard", False)
    cfg_has_skills = _has_skills_anywhere(agent_config)

    # main agent rubric (from config), decoupled from tools
    main_rubric_middleware = build_rubric_middleware(
        agent_config.rubric, ctx.model, workspace_root=ctx.workspace_dir, ctx=ctx
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
                ctx=ctx,
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
    # shell_guard + Jev 可用时值升级为 when 门控（dict 形态），否则 True 兜底。
    overlay = resolve_execute_interrupt(cfg_allow_shell, cfg_shell_guard, ctx.jev_gate_factory)
    if overlay is not None:
        main_agent_interrupt_on = {**(main_agent_interrupt_on or {}), **overlay}

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

    # KB 检索纪律（与 recursive 路径同语义：本函数组装的即主代理）
    if cfg_allow_knowledge:
        _kb = _kb_capability_prompt(
            agent_config.get("tool_names", []), getattr(ctx, "kb_cards", None)
        )
        if _kb:
            agent_config["system_prompt"] += _kb

    # jev kb 重排门挂载（主代理；与 recursive 路径同点位同条件）
    _maybe_append_jev_kb_gate(
        main_middleware, ctx, cfg_allow_knowledge, agent_config.get("tool_names", [])
    )

    # shell 虚实路径对齐提示：dictionary 路径 backend 为主代理统一构建（dicted
    # 子代理随父生效），提示同样只注入主代理 system_prompt（与 recursive 路径
    # 的 compiled per-node 语义对应）。
    _shell_note = _shell_workspace_note(cfg_allow_shell, cfg_workspace)
    if _shell_note:
        agent_config["system_prompt"] += _shell_note

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
