"""Middleware + backend builders (migrated from DeepAgentsDemos core_utils.py).

Changes:
- No sys.path hack / no base_confs global imports.
- `_build_backend` LOCAL_DIR hardcoded -> config-injected paths.
- `build_rubric_middleware` grader tools built from path-bound factory.
- Turn-scoped variants (TurnScopedRubricMiddleware / TurnScopedFixedRubricMiddleware):
  grading view is limited to the current turn (last real user message → end),
  see `_TurnScopedGraderMixin`.
- JEV 门控变体（TurnScopedJev*）：`_JevTriageMixin` 在上游 `_invoke_grader`/
  `_ainvoke_grader` 扩展点上做先验分诊（每次评分含首评：fast-pass 合成
  satisfied，其余委派 LLM；shadow 态只判不短路）。
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
from deepagents.middleware.rubric import GraderResponse, RUBRIC_GRADER_MESSAGE_SOURCE
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langchain_typesafe import Noul

from app.core.user_scoped_backend import TrackedUserScopedFilesystemBackend
from app.core.user_scoped_shell_backend import UserScopedLocalShellBackend
from app.tools.rubric_tools import make_grader_tools

logger = logging.getLogger(__name__)

try:  # 上游私有函数，可能随版本迁移；缺失时退回本地有界渲染
    from deepagents.middleware.rubric import _build_grader_transcript as _upstream_transcript
except ImportError:  # pragma: no cover - depends on installed versions
    _upstream_transcript = None


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


def current_turn_start(messages: list) -> int | None:
    """回合切片锚点：最后一条真实用户消息（非 grader 合成）的下标，无则 None。

    `_TurnScopedGraderMixin`（LLM 评分视图）与 JEV triage（送判 state 渲染）
    共用——两处切片逻辑必须一致，否则 JEV 判的回合与 LLM 评的回合错位
    （多轮串评 bug 的门控版复活）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and (
            getattr(m, "additional_kwargs", None) or {}
        ).get("lc_source") != RUBRIC_GRADER_MESSAGE_SOURCE:
            return i
    return None


class _TurnScopedGraderMixin:
    """评分材料限定为当前回合：最后一条真实用户消息 → 通道末尾。

    上游 RubricMiddleware 的评分视图是「强制前置首条用户消息 + 最近
    _MAX_TRANSCRIPT_MESSAGES 条」，为单任务轨迹设计。本平台把它用作
    会话级常驻评审（FixedRubricMiddleware 每回合注入固定 rubric），累积
    历史会随窗口滑入评审现场：首问题被强制前置而其原始回答滚出窗口，
    grader 据此判定"问题从未被回答"，needs_revision 的修订回路转而补答
    与本次提问无关的旧问题（实测「尼康相机学习上手」thread）。

    修复：覆写上游文档明示的 `_grader_input` 扩展点（rubric.py，同步/
    异步 grader 调用共用此入口），把传入 payload 构建器的 messages 视图
    换成当前回合切片。切片 = 最后一条「真实」HumanMessage（lc_source !=
    rubric_grader，即用户发起而非 grader 合成）到通道末尾：本轮提问、
    回答、工具调用、修订循环（grader 反馈 + 补答）全量保留，可被复审；
    更早回合全部排除。切片内首条 human 恰为本轮提问，上游的强制前置
    逻辑自然选中它——当前问题永远不会滚出自己的评分窗口。

    仅替换评审视图，不改 messages 通道真值：主智能体上下文、/history、
    withdraw 语义均不受影响。
    """

    def _grader_input(self, state, iteration, correction=None):
        msgs = state.get("messages", [])
        start = current_turn_start(msgs)
        if start is None or start == 0:
            # 无真实用户消息（异常形态）或本回合本就从头开始：维持原行为
            return super()._grader_input(state, iteration, correction)
        scoped = {**state}
        scoped["messages"] = msgs[start:]
        return super()._grader_input(scoped, iteration, correction)


class TurnScopedRubricMiddleware(_TurnScopedGraderMixin, RubricMiddleware):
    """RubricMiddleware + 回合级评分窗口（每消息 invoke payload rubric 路径）。"""


class TurnScopedFixedRubricMiddleware(_TurnScopedGraderMixin, FixedRubricMiddleware):
    """FixedRubricMiddleware + 回合级评分窗口（常驻 rubric 路径）。"""


# ======================== JEV rubric 门控（先验分诊，含首评）========================

# thread_id 提取：langgraph `Runtime` 不含 `.config`（官方文档明示须用
# `langgraph.config.get_config()`；shell 守卫的 `req.runtime` 是 ToolRuntime
# 子类才有 config）。`_invoke_grader` 运行在 after_agent 图任务上下文内，
# get_config() 直接返回该任务的 RunnableConfig——无需 after_agent 埋点透传。


def _fallback_transcript(messages: list) -> str:
    """上游 `_build_grader_transcript` 不可用时的本地有界渲染（防御性降级）。"""
    parts: list[str] = []
    for m in messages:
        role = getattr(m, "type", "message")
        content = str(getattr(m, "content", "") or "")
        if len(content) > 4000:
            content = content[:4000] + "...(truncated)"
        parts.append(f"[{role}] {content}")
    return "\n\n".join(parts) or "(empty transcript)"


def render_turn_transcript(messages: list) -> str:
    """回合切片 transcript（先切片再有界渲染）——JEV 送判 state 的素材。"""
    start = current_turn_start(messages)
    scoped = messages if start is None else messages[start:]
    if _upstream_transcript is not None:
        return _upstream_transcript(scoped)
    return _fallback_transcript(scoped)


def render_jev_rubric_state(state, rubric_text: str) -> str:
    """构建送判 state：回合切片 transcript + rubric + 冻结准则清单。

    与 `_TurnScopedGraderMixin` 共用同一回合切片（current_turn_start），
    保证 JEV 判的回合与 LLM 评的回合一致。state 全量送判不截断：超长由
    API 拒绝 → JevUnavailable → 委派 LLM grader（宁可不判，不发残卷）。
    """
    frozen = state.get("_rubric_criteria") or []
    transcript = render_turn_transcript(state.get("messages", []))
    blocks = [f"<rubric>\n{rubric_text}\n</rubric>"]
    if frozen:
        checklist = "\n".join(f"{i}. {name}" for i, name in enumerate(frozen, start=1))
        blocks.append(f"<criteria>\n{checklist}\n</criteria>")
    blocks.append(f"<transcript>\n{transcript}\n</transcript>")
    return "\n\n".join(blocks)


def serialize_jev_criteria(criteria) -> str:
    """criteria → 准则清单文本（content 为空时的 LLM 评审依据 / fixed 注入文本）。

    单一真相源形态（grader=jev 且未写 content）：LLM grader 与 JEV 评的是
    同一份准则序列化，二者天然对齐。
    """
    lines = [f"{i}. {c.statement}" for i, c in enumerate(criteria, start=1)]
    return "本 rubric 由结构化准则序列化生成，逐条判定：\n" + "\n".join(lines)


def render_jev_criteria(criteria, node_threshold: float | None = None):
    """JevCriterion[] → (API 问题集, 逐题合成元数据)。

    单一语义：每条准则一个 noul 题（statement=达标断言，P(yes) 高=满足）。
    qid 稳定为 c{index}（配置顺序即问题顺序；qid 只在单次判决内使用、
    不跨配置持久化，无迁移问题）。行级 threshold 覆盖节点级；None 交给
    build_rubric_judge 以平台级默认补齐（三级解析的最后一跳）。

    Returns:
        (questions, crit_meta)：questions 为 langchain_typesafe Noul 对象集
        （直接进 ClassifierRequest）；crit_meta 每项含 threshold/statement，
        供本地布尔合成。
    """
    questions: dict = {}
    crit_meta: dict[str, dict] = {}
    for i, c in enumerate(criteria):
        qid = f"c{i}"
        questions[qid] = Noul(instructions=c.statement)
        crit_meta[qid] = {
            "threshold": c.threshold if c.threshold is not None else node_threshold,
            "statement": c.statement,
        }
    return questions, crit_meta


class _JevTriageMixin:
    """JEV rubric 先验门控：`_invoke_grader`/`_ainvoke_grader` 方法位（上游钦定扩展点）。

    门控链（先验语义：每次评分前分诊，含首评 iteration 0）：
      L1 确定性直通（零成本短路 → 全部委派 LLM grader）：
        - correction 非 None（`_grade` 覆盖重试，重试必须真干活）；
        - 取证工具非空（grader_tool_names——证据型评审不可概率短路）。
      L2 JEV 判决：回合切片 state → RubricJudge.judge/ajudge（异步路径不阻塞
        事件循环）；判决异常已被 RubricJudge 归一为 unavailable（fail-closed）。
        问题集来自结构化 criteria（配置即契约），不依赖上游冻结机制——首评
        无冻结名单时合成响应用准则原文兜底，冻结名单由此锚定。
      L3 本地合成：fast_pass ∧ mode=enforce → 合成 satisfied（criteria 逐条
        CriterionPass，名字优先复用冻结名单——满足 `_usability_correction`
        覆盖率契约）；shadow/灰区/不可用 → 委派 super()（= 纯 llm 行为）。

    本 mixin 绝不向上抛异常：`after_agent` 会把异常记为 grader_error 终态，
    门控故障必须表现为"回落 LLM"而非"终止评审"。

    误放行风险（快道 = 该回答零 LLM 评审）由 per-agent grader=jev 自愿承担；
    平台级 jev_rubric_mode=shadow 为全局回退保险丝（只判只落审计，行为不变）。
    """

    # ---------------- L1：确定性直通判定（纯函数，便于单测）----------------
    @staticmethod
    def _jev_gate_open(state, correction) -> bool:
        return correction is None

    # ---------------- L2/L3 ----------------
    def _jev_judge_or_none(self):
        return getattr(self, "_jev_judge", None)

    def _synthesize_jev_response(self, decision, state):
        """RubricDecision → GraderResponse（满足上游一致性 + 覆盖率契约）。"""
        names = list(state.get("_rubric_criteria") or [])
        if not names:  # 防御：门控开启时冻结集必非空，此处兜底用准则原文
            judge = self._jev_judge_or_none()
            names = list(getattr(judge, "criteria_statements", None) or [])
        return GraderResponse.model_validate(
            {
                "result": "satisfied",
                "explanation": decision.annotation or "[Jev] 全准则达标（快道放行）",
                "criteria": [{"name": n, "passed": True} for n in names],
            }
        )

    def _invoke_grader(self, state, iteration, correction=None, *, context=None):
        judge = self._jev_judge_or_none()
        if (
            judge is not None
            and self._jev_gate_open(state, correction)
            and not getattr(self, "_tools", None)
        ):
            try:
                decision = judge.judge(
                    render_jev_rubric_state(state, state.get("rubric") or ""),
                    key=_jev_cache_key(state, iteration),
                    thread_id=_jev_thread_id(),
                )
            except Exception:  # noqa: BLE001 — 门控故障=回落 LLM，严禁 grader_error
                logger.exception("jev rubric triage raised (fail-closed → delegate LLM)")
            else:
                if (
                    decision.fast_pass
                    and getattr(judge, "mode", "enforce") != "shadow"
                ):
                    return self._synthesize_jev_response(decision, state)
        return super()._invoke_grader(state, iteration, correction, context=context)

    async def _ainvoke_grader(self, state, iteration, correction=None, *, context=None):
        judge = self._jev_judge_or_none()
        if (
            judge is not None
            and self._jev_gate_open(state, correction)
            and not getattr(self, "_tools", None)
        ):
            try:
                decision = await judge.ajudge(
                    render_jev_rubric_state(state, state.get("rubric") or ""),
                    key=_jev_cache_key(state, iteration),
                    thread_id=_jev_thread_id(),
                )
            except Exception:  # noqa: BLE001 — 门控故障=回落 LLM，严禁 grader_error
                logger.exception("jev rubric triage raised (fail-closed → delegate LLM)")
            else:
                if (
                    decision.fast_pass
                    and getattr(judge, "mode", "enforce") != "shadow"
                ):
                    return self._synthesize_jev_response(decision, state)
        return await super()._ainvoke_grader(state, iteration, correction, context=context)


def _jev_cache_key(state, iteration) -> tuple:
    """判决缓存键 (run_id, thread_id, iteration)。

    thread_id 入键：实测 iter≥1 偶发 `_current_grading_run_id` 丢失（空串），
    若键退化为 ('', iter) 会跨会话碰撞返回陈旧判决。thread_id 取自当前图任务
    上下文（`_jev_thread_id`，after_agent 任务内 get_config() 可读）。
    """
    return (
        str(state.get("_current_grading_run_id") or ""),
        _jev_thread_id(),
        int(iteration),
    )


def _jev_thread_id() -> str:
    """提取当前图任务的 thread_id（审计关联用，缺省空串）。

    langgraph `Runtime` 不含 `.config`（官方文档明示须用
    `langgraph.config.get_config()`，见模块头部注释）；`_invoke_grader` 运行在
    after_agent 图任务上下文内，get_config() 直接返回该任务的 RunnableConfig。
    图上下文外（单测/离线）返回空串——thread_id 仅审计关联用，允许 unknown。
    """
    try:
        from langgraph.config import get_config

        configurable = ((get_config() or {}).get("configurable") or {})
        return str(configurable.get("thread_id") or "")
    except Exception:  # noqa: BLE001 — 无图上下文属正常（离线/单测）
        return ""


class _JevRubricBase(_JevTriageMixin):
    """构造收口：jev 门控参数在此接收，其余 kwargs 上抛宿主（协作继承）。

    thresholds 烘焙在 RubricJudge 内（build_rubric_judge 三级解析），
    中间件只持判决器引用。
    """

    def __init__(self, *, jev_judge=None, **kwargs):
        super().__init__(**kwargs)
        self._jev_judge = jev_judge


class TurnScopedJevRubricMiddleware(_JevRubricBase, _TurnScopedGraderMixin, RubricMiddleware):
    """payload-rubric 路径 + 回合切片 + JEV 门控。

    MRO 上三个构件各占独立方法位：_JevTriageMixin(_invoke_grader) /
    _TurnScopedGraderMixin(_grader_input) / RubricMiddleware(其余全部)，
    互不遮蔽。jev_judge=None 时退化为 TurnScopedRubricMiddleware（纯防御，
    正常构建路径不会出现）。
    """


class TurnScopedJevFixedRubricMiddleware(
    _JevRubricBase, _TurnScopedGraderMixin, FixedRubricMiddleware
):
    """常驻 rubric 路径 + 回合切片 + JEV 门控（fixed 注入逻辑继承宿主）。"""


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
    ctx=None,
) -> Optional[RubricMiddleware]:
    """Build RubricMiddleware (or FixedRubricMiddleware) from a RubricConfig.

    Args:
        rubric_cfg: schema_utils.RubricConfig instance; None / 无 content 且无
            criteria -> None。
        default_model: grader_model fallback (string or BaseChatModel).
        workspace_root: path for building grader forensics tools.
        ctx: BuildContext；grader="jev" 时经 ctx.jev_rubric_factory 注入判决器
            （None=平台能力未启用 → 回落 llm 路径 + 构建期 warning，fail-visible）。

    Returns:
        RubricMiddleware | None
    """
    if rubric_cfg is None:
        return None
    criteria = getattr(rubric_cfg, "criteria", None) or []
    content = (rubric_cfg.content or "").strip()
    if not content and not criteria:
        return None

    grader_registry = make_grader_tools(workspace_root)
    grader_tools = [
        grader_registry[name]
        for name in (rubric_cfg.grader_tool_names or [])
        if name in grader_registry
    ]

    jev_factory = getattr(ctx, "jev_rubric_factory", None) if ctx is not None else None
    use_jev = getattr(rubric_cfg, "grader", "llm") == "jev"
    if use_jev and (jev_factory is None or not criteria):
        # 能力缺失/准则缺失 → 回落 llm。回落本身是设计（能力开关全局控制），
        # 但必须可见——"配置了 jev 但实际跑的是 llm"不允许静默发生。
        logger.warning(
            "rubric grader='jev' falls back to llm (factory=%s criteria=%d) — "
            "check jev_rubric_enabled/TYPESAFE_API_KEY/criteria",
            "missing" if jev_factory is None else "ready",
            len(criteria),
        )
        use_jev = False

    if use_jev and content:
        # 先验门控语义提示：快道只对结构化 criteria 出题；content 中未被 criteria
        # 编码的要求只在灰区轮由 LLM grader 执行。criteria 即机器契约。
        logger.info(
            "rubric grader='jev' with content+criteria: fast lane grades the %d "
            "structured criteria only; prose enforcement rides the LLM gray zone",
            len(criteria),
        )

    # 单一真相源形态：content 为空但有 criteria → 序列化为准则文本（LLM 评审
    # 与 JEV 快道评同一份清单）。content 非空时以其为准（criteria 为预筛）。
    rubric_text = content or (serialize_jev_criteria(criteria) if criteria else "")

    common = dict(
        model=default_model,
        tools=grader_tools or None,
        max_iterations=rubric_cfg.max_iterations,
        on_evaluation=_log_evaluation,
    )

    if use_jev and rubric_cfg.fixed:
        return TurnScopedJevFixedRubricMiddleware(
            jev_judge=jev_factory(rubric_cfg), fixed_rubric=rubric_text, **common
        )
    if use_jev:
        return TurnScopedJevRubricMiddleware(jev_judge=jev_factory(rubric_cfg), **common)

    if rubric_cfg.fixed:
        return TurnScopedFixedRubricMiddleware(fixed_rubric=rubric_text, **common)
    return TurnScopedRubricMiddleware(**common)


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
        # Tracked 子类仅做落盘埋点（diff 徽标数据源），写入语义 100% 继承。
        routes["/workspace/"] = TrackedUserScopedFilesystemBackend(
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
