"""astream -> SSE event conversion (plan §5.2).

Adapts the validated _stream_response pattern from
scripts/pg_compiled_subagent_test_run.py — replaces print() with SSE event
yields. All "message -> SSE event" mapping centralized here (plan §11.3.2).

HITL origins (declarative interrupt_on vs interrupt() primitive) are unified at
the SSE layer: both surface as `event: interrupt`, front-end does not
differentiate (plan §5.3).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)


def _path(namespace: tuple) -> str:
    return "/".join(namespace) if namespace else "root"


def _content_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)


def _extract_reasoning(msg) -> Optional[str]:
    """Best-effort 思考内容提取（reasoning / thinking）。

    覆盖常见约定：
    - OpenAI 兼容思考模型（GLM / DeepSeek / Qwen）：reasoning 落在
      additional_kwargs['reasoning_content']。
    - Anthropic 式 'thinking' 内容块。
    无思考内容时返回 None。
    """
    ak = getattr(msg, "additional_kwargs", None) or {}
    rc = ak.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                text = block.get("thinking") or block.get("text") or block.get("reasoning")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    return None


def _msg_to_events(msg, agent_path: str) -> list[SSEEvent]:
    """Map a single langgraph message to zero or more SSE events."""
    events: list[SSEEvent] = []
    if isinstance(msg, AIMessage):
        reasoning = _extract_reasoning(msg)
        if reasoning:
            events.append(SSEEvent(
                type="thinking",
                agent_path=agent_path,
                payload={"content": reasoning},
            ))
        if msg.tool_calls:
            for tc in msg.tool_calls:
                events.append(SSEEvent(
                    type="tool_call",
                    agent_path=agent_path,
                    payload={
                        "name": tc.get("name"),
                        "args": tc.get("args", {}),
                        "id": tc.get("id"),
                        "caller": msg.name or agent_path,
                    },
                ))
        if msg.content:
            events.append(SSEEvent(
                type="agent_message",
                agent_path=agent_path,
                payload={"content": _content_str(msg.content), "name": msg.name},
            ))
    elif isinstance(msg, ToolMessage):
        events.append(SSEEvent(
            type="tool_result",
            agent_path=agent_path,
            payload={
                "tool_call_id": msg.tool_call_id,
                "content": _content_str(msg.content),
                "name": msg.name,
            },
        ))
    # HumanMessage and other types are not forwarded (user input already known)
    return events


def _has_interrupt(state) -> bool:
    """True if the graph paused on a HITL interrupt."""
    for task in getattr(state, "tasks", []):
        if getattr(task, "interrupts", None):
            return True
    return False


def _extract_interrupt(state) -> dict:
    """Extract the interrupt payload (action_requests + review_configs)."""
    action_requests: list[dict] = []
    review_configs: list[dict] = []
    raw_values: list[dict] = []
    for task in getattr(state, "tasks", []):
        for intr in getattr(task, "interrupts", []) or []:
            val = intr.value if hasattr(intr, "value") else intr.get("value")
            raw_values.append(val if isinstance(val, dict) else {"value": val})
            if isinstance(val, dict):
                action_requests.extend(val.get("action_requests", []))
                review_configs.extend(val.get("review_configs", []))
    return {
        "action_requests": action_requests,
        "review_configs": review_configs,
        "raw": raw_values,
    }


class StreamAdapter:
    """Convert agent.astream(...) into an SSE event stream."""

    def __init__(self, max_invoke_retries: int = 2):
        self.max_invoke_retries = max_invoke_retries

    async def stream_message(
        self,
        agent: CompiledStateGraph,
        content: str | list,
        config: dict,
        rubric: Optional[str] = None,
    ) -> AsyncIterator[SSEEvent]:
        """Stream a fresh user message. Yields until done / interrupt / error.

        content may be a plain str (text) or a multimodal list (text + image_url
        blocks) assembled by ContentComposer.
        """
        msg = HumanMessage(content=content)
        payload: dict = {"messages": [msg]}
        if rubric:
            payload["rubric"] = rubric
        async for ev in self._run(agent, payload, config):
            yield ev

    async def stream_resume(
        self,
        agent: CompiledStateGraph,
        decisions: list[dict],
        config: dict,
    ) -> AsyncIterator[SSEEvent]:
        """Resume after a HITL interrupt with caller decisions."""
        payload = Command(resume={"decisions": decisions})
        async for ev in self._run(agent, payload, config):
            yield ev

    async def _run(
        self,
        agent: CompiledStateGraph,
        payload,
        config: dict,
    ) -> AsyncIterator[SSEEvent]:
        try:
            while True:
                async for namespace, update in agent.astream(
                    payload,
                    config=config,
                    stream_mode="updates",
                    subgraphs=True,
                ):
                    agent_path = _path(namespace)
                    if not isinstance(update, dict):
                        continue
                    for _node_name, state in update.items():
                        if not isinstance(state, dict):
                            continue
                        for msg in state.get("messages", []):
                            for ev in _msg_to_events(msg, agent_path):
                                yield ev

                state = await agent.aget_state(config)

                if state.next:
                    if _has_interrupt(state):
                        # HITL pause — push interrupt event, end stream.
                        # Front-end POSTs /resume when ready.
                        yield SSEEvent(
                            type="interrupt",
                            payload=_extract_interrupt(state),
                        )
                        return
                    # Non-interrupt suspension (rare): continue streaming.
                    payload = None
                    continue
                else:
                    yield SSEEvent(type="done", payload={})
                    return
        except Exception as e:
            logger.exception("stream error: %s", e)
            # Error floor: always close the stream with an error event so the
            # front-end can determine final state (plan §11.2.3).
            yield SSEEvent(type="error", payload={"message": str(e)})
