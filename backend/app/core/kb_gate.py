"""KB 检索前置门（kb v2 门控面：无检索证据不得直接作答）。

纯提示触发检索率 <100%（plan 风险 R3）——本门为确定性兜底，不依赖提示
服从：agent 自然停机（终答候选、无后续工具调用）时检查本回合内是否存在
`{kb_tool}` 调用证据，无则注入系统提醒并 `jump_to=model` 强制续跑检索
（rubric needs_revision 同款回环机制）。

- 提醒上限 `_MAX_REMINDERS` 次后放行（fail-open，防死循环）+ warning；
- 提醒消息打 `lc_source` 合成标记：回合锚点定位排除合成消息（与 rubric
  grader 同纪律），且提醒计数直接由消息扫描得出，不引入新 state 通道；
- 门控永不抛出：任何内部异常兜底为放行（检索证据判定失败不能阻断回答）。
"""
from __future__ import annotations

import logging

from deepagents.middleware.rubric import RUBRIC_GRADER_MESSAGE_SOURCE
from langchain.agents.middleware import hook_config
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)

# 合成消息来源标记（计入提醒次数；回合锚点定位须跳过）
KB_GATE_MESSAGE_SOURCE = "kb_gate_reminder"

_SYNTHETIC_SOURCES = frozenset({RUBRIC_GRADER_MESSAGE_SOURCE, KB_GATE_MESSAGE_SOURCE})
_MAX_REMINDERS = 2

_REMINDER_TEXT = (
    "（系统提醒）你绑定了知识库，但本回合尚未调用 {tool} 检索。请先调用 {tool} "
    "（kb_ids 只能使用工具描述列出的真实 kb_id），依据检索结果作答后再收束；"
    "若你判定问题确实与绑定知识库无关，请直接给出最终回答。"
)


def _turn_anchor(messages: list) -> int | None:
    """回合切片锚点：最后一条真实用户消息下标（跳过 rubric/kb 门合成消息）。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and (
            getattr(m, "additional_kwargs", None) or {}
        ).get("lc_source") not in _SYNTHETIC_SOURCES:
            return i
    return None


def _has_kb_evidence(turn: list, kb_tool: str) -> bool:
    """回合内是否存在 kb_search 调用证据（工具调用声明或工具结果任一命中）。"""
    for m in turn:
        if isinstance(m, AIMessage):
            for tc in (getattr(m, "tool_calls", None) or []):
                if isinstance(tc, dict) and tc.get("name") == kb_tool:
                    return True
        elif isinstance(m, ToolMessage) and getattr(m, "name", None) == kb_tool:
            return True
    return False


def _reminder_count(turn: list) -> int:
    return sum(
        1
        for m in turn
        if (getattr(m, "additional_kwargs", None) or {}).get("lc_source")
        == KB_GATE_MESSAGE_SOURCE
    )


class KbPrerequisiteMiddleware(AgentMiddleware):
    """检索前置门（主代理独占，与知识集提示同点位同条件挂载）。"""

    def __init__(self, kb_tool: str):
        super().__init__()
        self._kb_tool = kb_tool

    @property
    def name(self) -> str:
        return f"KbPrerequisiteMiddleware({self._kb_tool})"

    def _check(self, state) -> dict | None:
        """纯逻辑（同步/异步共用）：无证据 → 注入提醒回环；超限/异常 → 放行。"""
        try:
            messages = state.get("messages", []) or []
            anchor = _turn_anchor(messages)
            if anchor is None:
                return None
            turn = messages[anchor:]
            if _has_kb_evidence(turn, self._kb_tool):
                return None
            count = _reminder_count(turn)
            if count >= _MAX_REMINDERS:
                logger.warning(
                    "kb gate: %d 次提醒后仍无 %s 检索证据，放行终答（fail-open）",
                    count, self._kb_tool,
                )
                return None
            logger.info("kb gate: 本回合无检索证据，注入第 %d 次检索提醒", count + 1)
            return {
                "jump_to": "model",
                "messages": [
                    HumanMessage(
                        content=_REMINDER_TEXT.format(tool=self._kb_tool),
                        additional_kwargs={"lc_source": KB_GATE_MESSAGE_SOURCE},
                    )
                ],
            }
        except Exception:  # noqa: BLE001 — 门控自身故障绝不阻断回答
            logger.warning("kb gate check failed (passthrough)", exc_info=True)
            return None

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state, runtime) -> dict | None:
        return self._check(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_agent(self, state, runtime) -> dict | None:
        return self._check(state)


__all__ = ["KbPrerequisiteMiddleware", "KB_GATE_MESSAGE_SOURCE"]
