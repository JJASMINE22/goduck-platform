"""Agent config schema + YAML/JSON loaders (migrated from DeepAgentsDemos).

The AgentConfig tree is the single source of truth frozen into agent.config_json.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class JevCriterion(BaseModel):
    """一条结构化判决准则（grader=jev 时渲染为 Jev 二元问题）。

    仅"满足时通过"语义：statement 是可从对话记录判真假的**达标断言**——
    P(yes) 高 = 准则满足。危险类要求写成正向达标式（"引用真实检索结果"
    而非"不编造"），措辞方向与判定方向天然一致，无极性登记心智负担。
    """

    statement: str = Field(
        min_length=1,
        description="陈述性达标断言：仅凭当前回合 transcript 即可核实答案的可观察属性，"
        "如'回答中每个统计数据均标注了来源'（非形容词、非否定句；不要写需要访问"
        "项目文件或外部事实才能验证的准则——无状态判决器本质不可验证）。",
    )
    threshold: Optional[float] = Field(
        default=None,
        ge=0.5,
        lt=1.0,
        description="行级阈值覆盖（P 与 conf 双轴同值）；缺省回落节点级 pass_threshold"
        "→平台级全局默认。",
    )
    yes: Optional[str] = Field(
        default=None,
        description="成立侧界定（可选）：覆盖默认的'回答中明确成立且仅凭对话可核实'。",
    )
    no: Optional[str] = Field(
        default=None,
        description="不成立侧界定（可选）：覆盖默认的'未体现或需对话之外的事实'。",
    )


class RubricConfig(BaseModel):
    """Per-agent-node rubric self-evaluation config.

    Mounted where it lives, it grades where it lives:
    - main gateway: grades final user-facing reply (incl. subagent tool messages).
    - subagent: self-evaluates inside its own graph; only handed back to parent once satisfied.

    grader 两态（plan: jev-rubric-grader-gate）：
    - llm（默认）：现状 LLM grader 子代理评审，行为不变；
    - jev：JEV 门控分诊——高置信达标（全部准则 P/conf 过阈值）合成 satisfied
      跳过 LLM grader；灰区/不可用/带取证工具/覆盖重试一律委派 LLM（fail-closed）。
      仅常驻模式可用（问题集由 criteria 在建图时固定）；需后端 jev 能力
      （jev_rubric_enabled ∧ TYPESAFE_API_KEY），缺失时构建回落 llm 并告警。

    fixed 两态决定评审依据的来源：
    - fixed=false（按需）：准则全部来自会话内每次调用传入的 rubric，content 无需
      填写；不传则该回合不评审。
    - fixed=true（常驻）：content（或 jev 的 criteria）必填，作为无显式 rubric 时的
      注入准则。
    """

    content: Optional[str] = Field(
        default=None,
        description="评分准则文本（LLM grader 的评审依据）。fixed=true 时必填"
        "（常驻注入文本；grader=jev 可只写 criteria，平台自动序列化为准则文本，"
        "单一真相源）；fixed=false 时忽略——评审依据全部来自会话内传入的 rubric。"
        "与 criteria 并存时 content 为 LLM 评审真相、criteria 为 JEV 快道预筛"
        "（分歧最坏=多一次 LLM 调用，不损害正确性）。",
    )
    fixed: bool = Field(
        default=False,
        description="True=常驻 rubric (FixedRubricMiddleware, 无显式 rubric 时注入); "
        "False=按需 rubric：中间件始终挂载，仅当调用方在 invoke payload 中传 rubric "
        "时评审（不传则本回合不触发），content 无需填写。",
    )
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=20,
        description="单次 rubric 尝试的最大评审轮数, 上限 20。",
    )
    grader_tool_names: List[str] = Field(
        default_factory=list,
        description="grader 可调用的取证工具名 (从 GRADER_TOOL_REGISTRY 解析), 与主代理工具集无关。"
        "非空时 JEV 门控不参与（证据型评审不可概率短路，构建期直通 LLM）。",
    )
    grader: Literal["llm", "jev"] = Field(
        default="llm",
        description="grader 模式：llm=现状 LLM 评审；jev=JEV 门控分诊"
        "（仅 fixed=true 可用，需配置 criteria）。",
    )
    criteria: Optional[List[JevCriterion]] = Field(
        default=None,
        description="结构化判决准则集（grader=jev 必填）。逐条渲染为 Jev 问题。",
    )
    pass_threshold: Optional[float] = Field(
        default=None,
        ge=0.5,
        lt=1.0,
        description="节点级快道阈值（P 与 conf 双轴同值，即'倾向性'：高=保守多走灰区，"
        "低=激进多走快道）；缺省回落平台级全局默认。行级 threshold 可再覆盖。",
    )

    @model_validator(mode="after")
    def _validate_rubric(self) -> "RubricConfig":
        if self.grader == "jev":
            if not self.fixed:
                raise ValueError(
                    "grader='jev' 仅支持常驻模式（fixed=true）：JEV 只能对预先枚举的准则出题，"
                    "不接受会话内传入的 rubric"
                )
            if not self.criteria:
                raise ValueError(
                    "grader='jev' 需要配置 criteria（结构化判决准则集，JEV 只能对已枚举的命题出题）"
                )
        if self.fixed and not ((self.content or "").strip() or self.criteria):
            raise ValueError(
                "rubric fixed=true（常驻模式）必须填写评分准则 content"
                "（或 grader=jev 的 criteria）——常驻模式需有可注入的固定准则"
            )
        return self


class AgentConfig(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = Field(default=None, min_length=1)
    system_prompt: str = Field(..., min_length=1)
    tool_names: List[str] = Field(default_factory=list)
    skills: Optional[List[str]] = Field(default=None)
    memory: bool = Field(
        default=False,
        description="是否启用偏好记忆（仅主代理）。True 时挂载 /memories/ 路由并启用 "
        "MemoryMiddleware；具体偏好文件由系统提示约定（如 /memories/preferences.md）。"
        "旧配置的非空 list 自动迁移为 True。",
    )
    workspace: bool = Field(
        default=False,
        description="是否启用工作区。True 时挂载 /workspace/ 路由到 "
        "LOCAL_ROOT/workspace（目录路径由后端固定，前端只开关）。"
        "主代理与 compiled 子代理可开启：开启后该节点获得独立磁盘后端，与主代理共用同一磁盘"
        "目录，写入即落盘、跨代理可见。dicted 子代理共享主代理 backend（随父生效），"
        "本开关不参与构建，配置了会被校验层剥离为 False。"
        "旧配置的非空 str 自动迁移为 True。",
    )
    interrupt_on: Optional[Dict[str, bool]] = Field(
        default=None,
        description="工具使用审批（HITL）映射：工具名 → 是否需要人工审批。仅真值条目生效"
        "（False=显式不审批）。usage=cron 整树禁用（无人值守无审批通道，工具自动执行）。",
    )
    is_main: bool = False
    is_dicted: bool = False
    subagents: Optional[List[AgentConfig]] = None
    dynamic_dispatch: bool = Field(
        default=False,
        description="该节点启用 CodeInterpreterMiddleware, 通过 JS 解释器的 task() 动态编排"
        "其直接子代理 (循环/分支/并行), 而非静态逐个 task 委派。"
        "约束: 必须下挂子代理; 下挂子代理必须统一为 dicted 叶子 (无 subagents, "
        "锁死两层拓扑); dicted 叶子支持 responseSchema 结构化输出。",
    )
    rubric: Optional[RubricConfig] = None
    allow_attachments: bool = Field(
        default=False,
        description="是否启用附件/多模态能力开关。模型多模态能力由 model registry 元数据驱动。",
    )
    allow_knowledge: bool = Field(
        default=False,
        description="是否启用知识库能力（Agentic RAG）。仅主代理可开。开启后经"
        " kb_ids 绑定智能体级知识集，agent 持 MCP 工具 <注册名>__kb_search"
        " 自主检索（服务端校验 kb_ids 必须落在绑定集合内）。",
    )
    kb_ids: List[str] = Field(
        default_factory=list,
        description="智能体级知识集（kb_id 多选，kb v2）。仅主代理可设、随配置"
        "持久化；检索工具按集合校验传入 kb_ids（集合外视为编造拒绝）。"
        "空集 = 未绑定（能力开启也无从检索）。",
    )
    allow_shell: bool = Field(
        default=False,
        description="是否启用本地 shell 执行能力（execute 工具）。主代理与子代理均可设（与 workspace 同构，"
        "per-node：设了该开关的节点获得自己的 UserScopedLocalShellBackend）。"
        "True 时挂载 per-user cwd 后端，并强制 interrupt_on={'execute': True} 每条命令人工审批。"
        "注意：dicted 子代理共享父代理 backend（随父生效），compiled 子代理独立（需自身开启）。",
    )
    shell_guard: bool = Field(
        default=False,
        description="是否启用 Jev shell 守卫（allow_shell 的 HITL 减负）。仅在 allow_shell=True 且"
        "后端 jev_enabled 时生效：只读白名单静默直通；灰区命令由 Jev 概率门控决定"
        "'是否跳过人工'（shadow 只记录标注、enforce 高置信常规范围自动放行）；"
        "包安装/高危/判定失败一律问人（fail-closed）。dicted 叶子共享父 backend，不生效。",
    )
    usage: Literal["chat", "cron"] = Field(
        default="chat",
        description="用途声明（仅根节点/主代理可设，随 config_json 持久化，纯元数据不参与图构建）。"
        "'cron'=定时任务智能体：无人值守无审批通道，全树禁止 allow_shell 与 interrupt_on，"
        "根节点禁止 allow_attachments/allow_knowledge（定时任务输入为固定 prompt，附件/知识库"
        "通道暂不设计）。"
        "绑定层以 agent.kind 为准（cron_service 仍独立复查配置树，覆盖先绑定后改配置的时间差）。",
    )
    task_planning: bool = Field(
        default=False,
        description="是否启用任务规划（TodoListMiddleware）。True 时该节点获得 write_todos 工具与 "
        "todos 状态通道，执行中通过 SSE todos 事件推送清单进度。主代理与子代理均可独立开启"
        "（不继承：各节点的 todos 存在于各自的 graph state，互不可见）。"
        "工具无关（不包裹既有工具调用，不影响 HITL），dicted 叶子亦可开启。",
    )

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for s in v:
            if not re.match(r"^/skills/[^/]+/$", s):
                raise ValueError(f"技能路径格式错误: '{s}', 期望格式 '/skills/<name>/'")
        return v

    @field_validator("memory", "workspace", mode="before")
    @classmethod
    def _coerce_legacy_bool(cls, v):
        """平滑迁移旧配置到 bool：非空 str/list → True，None/空 → False。

        DB 中旧 agent 配置可能仍存 memory=['/memories/...'] / workspace='workspace'，
        此处一律归一为开关语义，无需 DB 迁移；下次保存即落库为 bool。
        """
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip() != ""
        if isinstance(v, list):
            return len(v) > 0
        return bool(v)

    @model_validator(mode="after")
    def validate_dicted_backend_attrs(self) -> AgentConfig:
        """dicted 叶子的后端三键（workspace/allow_shell/shell_guard）剥离。

        构建路径上 dicted 分支零读取这三个键（backend 由主代理 cfg 统一构建，
        dicted 子代理经 FilesystemMiddleware(backend=父backend) 随父生效），
        配置了只是"看起来可配"的惰性假象。此处静默置 False + warning
        （fail-visible，与 usage=cron 的构建期剥离同构）而非硬拒绝——硬拒绝会把
        存量惰性配置（如 DB 直写的 writer allow_shell+shell_guard 空转对）卡死在
        保存环节，而其运行时行为本就无差。

        顺序约束：必须先于 validate_topology 的 shell_guard→allow_shell 依赖校验
        执行（pydantic 同 mode 多 validator 按定义序运行），否则"dicted 且
        shell_guard=true 且 allow_shell=false"的存量脏形态会先撞依赖校验而无法净化。
        """
        if self.is_dicted and not self.is_main:
            stripped = [k for k in ("workspace", "allow_shell", "shell_guard") if getattr(self, k)]
            if stripped:
                self.workspace = False
                self.allow_shell = False
                self.shell_guard = False
                logger.warning(
                    "dicted 子代理 %r 的后端属性 %s 随主代理生效（节点配置不参与构建），已剥离为 False",
                    self.name,
                    stripped,
                )
        return self

    @model_validator(mode="after")
    def validate_topology(self) -> AgentConfig:
        if self.is_main:
            if self.name is not None:
                raise ValueError("主代理(is_main=True)不需要设置 name")
            if self.description is not None:
                raise ValueError("主代理(is_main=True)不需要设置 description")
        else:
            if not self.name:
                raise ValueError("子代理必须设置 name")
            if not self.description:
                raise ValueError("子代理必须设置 description")
            if self.memory:
                raise ValueError("子代理不能设置 memory 偏好文件")
            if self.allow_attachments:
                raise ValueError("子代理不能设置 allow_attachments 附件能力")
            if self.allow_knowledge:
                raise ValueError("子代理不能设置 allow_knowledge 知识库能力")
            if self.kb_ids:
                raise ValueError("子代理不能设置 kb_ids 知识集（仅主代理可绑定）")
            if self.usage != "chat":
                raise ValueError(
                    "子代理不能设置 usage 用途声明（仅主代理/根节点可设，整树共用）"
                )

        if self.shell_guard and not self.allow_shell:
            raise ValueError(
                f"{'主代理' if self.is_main else f'子代理 {self.name!r}'} 启用了 shell_guard"
                " 但未开启 allow_shell（守卫只对 execute 命令有意义）"
            )

        # cron 用途整树禁 shell（无人值守 + 非沙箱 + HITL 死锁三重矛盾）。
        # 在根 validator 完成整树检查（子代理节点自身看不到兄弟节点）。
        if self.is_main and self.usage == "cron" and _tree_allows_shell_node(self):
            raise ValueError(
                "定时任务智能体（usage=cron）不允许任何节点开启 allow_shell"
                "（无人值守无审批通道）"
            )

        # cron 用途收紧（同 shell 禁令的根 validator 整树检查）：无人值守下
        # HITL 只会永久挂起任务，故禁 interrupt_on；附件/知识库依赖会话消息
        # 装配通道（固定 prompt 无此通道），暂不设计，故禁两开关。
        if self.is_main and self.usage == "cron":
            if self.allow_attachments:
                raise ValueError(
                    "定时任务智能体（usage=cron）不允许开启 allow_attachments 附件能力"
                    "（任务输入为固定 prompt，无附件通道）"
                )
            if self.allow_knowledge:
                raise ValueError(
                    "定时任务智能体（usage=cron）不允许开启 allow_knowledge 知识库能力"
                    "（定时任务暂不支持知识库，待后续版本设计）"
                )
            if self.kb_ids:
                raise ValueError(
                    "定时任务智能体（usage=cron）不允许绑定 kb_ids 知识集"
                    "（定时任务暂不支持知识库）"
                )
            hitl_labels = _tree_interrupt_labels(self)
            if hitl_labels:
                raise ValueError(
                    "定时任务智能体（usage=cron）不允许配置工具使用审批（interrupt_on，"
                    "无人值守无审批通道，工具一律自动执行）：" + "；".join(hitl_labels)
                )

        if self.is_main and self.is_dicted:
            raise ValueError("主代理(is_main=True)不能为 dicted 模式")

        if self.is_dicted and self.subagents:
            raise ValueError(
                f"子代理 '{self.name}' 为 dicted 模式, 不允许嵌套 subagents"
            )

        if self.is_dicted and self.dynamic_dispatch:
            raise ValueError(
                f"子代理 '{self.name}' 为 dicted (叶子) 模式, 不能启用 dynamic_dispatch"
            )

        if self.dynamic_dispatch:
            label = "主代理" if self.is_main else f"子代理 '{self.name}'"
            if not self.subagents:
                raise ValueError(
                    f"{label} 启用了 dynamic_dispatch, 但未下挂任何 subagents"
                    " (动态编排必须有可调度的下游)"
                )
            for sa in self.subagents:
                if not sa.is_dicted:
                    raise ValueError(
                        f"{label} 启用了 dynamic_dispatch, 下挂子代理 '{sa.name}' 必须为 "
                        "dicted (叶子) 模式, 但当前为 compiled"
                        " (动态子代理下游统一 dicted 叶子: 锁死两层拓扑 + 支持 responseSchema)"
                    )

        return self

    @model_validator(mode="after")
    def validate_subagents(self) -> AgentConfig:
        if self.subagents:
            for sa in self.subagents:
                if sa.is_main:
                    raise ValueError("子代理不能设置 is_main=True")
                if sa.memory:
                    raise ValueError("子代理不能设置 memory 偏好文件")
                if sa.allow_attachments:
                    raise ValueError("子代理不能设置 allow_attachments 附件能力")
                if sa.allow_knowledge:
                    raise ValueError("子代理不能设置 allow_knowledge 知识库能力")
                if sa.kb_ids:
                    raise ValueError("子代理不能设置 kb_ids 知识集")

        return self

    @property
    def allows_session_rubric(self) -> bool:
        """会话内按次传入的 rubric 对本节点是否生效。

        当且仅当启用 rubric 且为按需模式（fixed=false）：此时中间件已挂载且只认
        payload rubric。未启用时没有中间件消费该字段；常驻模式的准则已写死。
        能力端点（rubric-capability）与消息校验（validate_message_content）共用。
        """
        return self.rubric is not None and not self.rubric.fixed

    def to_dict(self) -> dict:
        result = {}
        for key, value in self:
            if value is None:
                continue
            # kb_ids 空集不落库：与 skills/None 字段同款省略语义，保持存量
            # config_json 干净（历史快照/导出 YAML 无空数组噪音）
            if key == "kb_ids" and not value:
                continue
            if key == "subagents" and value:
                result[key] = [sa.to_dict() for sa in value]
            elif key == "rubric" and value is not None:
                result[key] = value.model_dump(exclude_none=True)
            else:
                result[key] = value
        return result


# ======================== Loaders ========================

def _tree_allows_shell_node(node: "AgentConfig") -> bool:
    """递归检查 AgentConfig 树上任一节点 allow_shell=True。"""
    if node.allow_shell:
        return True
    return any(_tree_allows_shell_node(sa) for sa in (node.subagents or []))


def _node_interrupt_tools(node: "AgentConfig") -> list[str]:
    """节点上取值为真的 interrupt_on 工具名（False 条目=显式不审批，无 HITL 语义）。"""
    io = node.interrupt_on
    if not io:
        return []
    return sorted(k for k, v in io.items() if v)


def _tree_interrupt_labels(node: "AgentConfig") -> list[str]:
    """递归收集树上配置了工具审批的节点描述（"主代理: t1,t2" / "子代理 'n': t1"）。"""
    labels: list[str] = []
    tools = _node_interrupt_tools(node)
    if tools:
        label = "主代理" if node.is_main else f"子代理 {node.name!r}"
        labels.append(f"{label}: {','.join(tools)}")
    for sa in node.subagents or []:
        labels.extend(_tree_interrupt_labels(sa))
    return labels


def tree_allows_shell(config: dict) -> bool:
    """递归检查 config_json（dict 形态）树上任一节点 allow_shell 为真。

    绑定层（cron_service/cron_scheduler/启动扫描）的共享判定：以能力为准、
    不以声明为准——覆盖"先绑定后改配置"的时间差与 usage=chat 智能体被复用
    的情况（plan §七.③）。对畸形输入（非 dict/缺键）返回 False（视为无 shell）。
    """
    if not isinstance(config, dict):
        return False
    if config.get("allow_shell"):
        return True
    return any(tree_allows_shell(sa) for sa in (config.get("subagents") or [])
               if isinstance(sa, dict))


def cron_unattended_violations(config: dict) -> list[str]:
    """config_json（dict 形态）树上收集 cron 无人值守禁用能力（审批/附件/知识库）。

    与 tree_allows_shell 同角色：绑定层（cron_service/cron_scheduler）以能力为准的
    共享判定，覆盖"先绑定后改配置"的时间差与直写 DB 旁路（validator 为第一道门）。
    返回人类可读的违规描述列表，空列表=干净；对畸形输入（非 dict/缺键）返回 []。
    注意：不含 allow_shell——shell 由 tree_allows_shell 单独判定（历史消息契约不变）。
    """
    violations: list[str] = []
    if not isinstance(config, dict):
        return violations
    io = config.get("interrupt_on")
    if isinstance(io, dict):
        tools = sorted(k for k, v in io.items() if v)
        if tools:
            violations.append(f"interrupt_on({','.join(tools)})")
    if config.get("allow_attachments"):
        violations.append("allow_attachments")
    if config.get("allow_knowledge"):
        violations.append("allow_knowledge")
    for sa in config.get("subagents") or []:
        if isinstance(sa, dict):
            violations.extend(cron_unattended_violations(sa))
    return violations


def load_config_from_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML 配置文件根节点必须是 dict, 实际类型: {type(data).__name__}")
    return data


def parse_config(config: dict) -> AgentConfig:
    """Validate a plain dict (JSON or YAML) into AgentConfig.

    100% reused by POST /topologies/validate (plan §11.3 — no rule rewrites).
    """
    return AgentConfig.model_validate(config)


def parse_yaml_config(path: str | Path) -> AgentConfig:
    return parse_config(load_config_from_yaml(path))


def config_to_yaml(agent_config: AgentConfig | dict) -> str:
    """Export config tree as YAML (for /topologies/{id}/export.yaml)."""
    data = agent_config.to_dict() if isinstance(agent_config, AgentConfig) else agent_config
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
