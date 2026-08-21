"""astream -> SSE event conversion (plan §5.2).

Streams in two modes simultaneously to deliver **token-level** output:
- stream_mode="messages" : LLM token deltas  -> `token` events (live content + reasoning)
- stream_mode="updates"  : node-complete msgs -> `tool_call` / `tool_result`
  (and `thinking` / `agent_message` as an atomic fallback for any AI message
  that was NOT streamed token-by-token).
  Node updates carrying a `todos` state change (TodoListMiddleware, per-node
  task planning) additionally emit a `todos` event — deduped per agent_path.

Dedup: every AI message delivered as tokens is recorded in `streamed_ids`
(matched by message id). In updates mode its content/reasoning are skipped to
avoid double-emitting, while its tool_calls still emit (those are not streamed).
This keeps the existing event contract intact — `tool_call` / `tool_result` /
`interrupt` / `done` / `error` behave exactly as before; `token` is the only
new event, and content/reasoning now flow through it when the provider streams.

HITL origins (declarative interrupt_on vs interrupt() primitive) are unified at
the SSE layer: both surface as `event: interrupt`, front-end does not
differentiate (plan §5.3).

File-change badges: `_run` activates the per-task collector for the whole
stream and drains it after each updates chunk (plus final drains before
done/interrupt/error), emitting `file_change` events whose payload carries
per-file line-diff records written by the tracked workspace backend at the
moment of disk mutation (see app/core/file_change_tracker.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core import file_change_tracker
from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)


def _path(namespace: tuple) -> str:
    return "/".join(namespace) if namespace else "root"


def _content_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)


def _text_delta(content: Any) -> str:
    """Plain-text fragment carried by a streaming chunk's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_reasoning(msg) -> Optional[str]:
    """Best-effort 思考内容提取（reasoning / thinking）for a COMPLETE message.

    Used only for the atomic fallback path (messages that weren't streamed).
    Covers GLM/DeepSeek/Qwen (additional_kwargs['reasoning_content']) and
    Anthropic-style 'thinking' content blocks. None when no reasoning present.
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


def _reasoning_delta(chunk) -> str:
    """Reasoning fragment from a streaming chunk (GLM/DeepSeek reasoning_content).

    langchain merges additional_kwargs string values via `+=` across chunks
    (see thinking_patch.py), so per-chunk deltas accumulate correctly.
    """
    ak = getattr(chunk, "additional_kwargs", None) or {}
    rc = ak.get("reasoning_content")
    return rc if isinstance(rc, str) and rc else ""


def _stream_id(chunk, meta: dict, agent_path: str) -> str:
    """Stable id for one streaming message (front-end aggregation + dedup key).

    Prefers the provider message id (consistent across chunks and the final
    node-complete message); falls back to a (path, node, step) synthetic key.
    """
    cid = getattr(chunk, "id", None)
    if cid:
        return str(cid)
    return f"{agent_path}:{meta.get('langgraph_node', '?')}:{meta.get('langgraph_step', '?')}"


def _normalize_todos(raw: Any) -> Optional[list[dict]]:
    """Normalize a `todos` state-channel value into [{content, status}] dicts.

    TodoListMiddleware persists todos in graph state; versions differ on entry
    shape (plain dict vs attribute object), so both are handled defensively.
    Returns None when the value is not a list (key absent / unexpected type).
    """
    if not isinstance(raw, list):
        return None
    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, dict):
            content = entry.get("content")
            status = entry.get("status")
        else:
            content = getattr(entry, "content", None)
            status = getattr(entry, "status", None)
        items.append({
            "content": content if isinstance(content, str) else str(content or ""),
            "status": status if isinstance(status, str) else str(status or "pending"),
        })
    return items


def _msg_to_events(msg, agent_path: str, streamed_ids: set[str]) -> list[SSEEvent]:
    """Map a node-complete message to zero or more SSE events.

    tool_call / tool_result always emit (not token-streamed). thinking /
    agent_message emit ONLY when the message was NOT delivered as tokens
    (recorded in streamed_ids) — otherwise they were already sent as `token`
    events and re-emitting would duplicate them.
    """
    events: list[SSEEvent] = []
    if isinstance(msg, AIMessage):
        already_streamed = bool(msg.id) and str(msg.id) in streamed_ids

        reasoning = _extract_reasoning(msg)
        if reasoning and not already_streamed:
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

        if msg.content and not already_streamed:
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
        upload_ids: Optional[list[str]] = None,
        raw_content: Optional[str] = None,
    ) -> AsyncIterator[SSEEvent]:
        """Stream a fresh user message. Yields until done / interrupt / error.

        content may be a plain str (text) or a multimodal list (text + image_url
        blocks) assembled by ContentComposer.

        upload_ids / raw_content are stashed on the message's additional_kwargs
        so the checkpointer persists them alongside content (they never enter
        the model payload — the model only sees ``content``):
        - upload_ids: recovered on /history to render attachment thumbnails
          (read directly, never parsed out of content).
        - raw_content: the user's original input. Only attachment messages have
          their content rewritten by ContentComposer (anchor table injected);
          for those we keep the original text so history shows the user's real
          message instead of the internal prompt scaffold. Plain messages have
          content == raw_content, so we skip the redundant copy.
        """
        additional_kwargs: dict[str, Any] = {}
        if upload_ids:
            additional_kwargs["upload_ids"] = upload_ids
            if isinstance(raw_content, str) and raw_content:
                additional_kwargs["raw_content"] = raw_content
        msg = HumanMessage(content=content, additional_kwargs=additional_kwargs)
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
        # ids of AI messages already delivered token-by-token (dedup vs updates)
        streamed_ids: set[str] = set()
        # agent_path -> last emitted todos (dedup: node updates may repeat the
        # same todos value; only state CHANGES are pushed as SSE events)
        last_todos: dict[str, list[dict]] = {}
        # 文件变更收集器引用：activate() yield 出实例，error 路径（with 已退出、
        # contextvar 已复位）仍可 drain(collector) 取走残留记录。
        collector = None
        agent_path = "root"
        try:
            # activate 必须包住整个 astream 迭代（含 while 外层）：并行子代理的
            # 子 task 在迭代中创建，copy_context 快照必须已含 collector。
            with file_change_tracker.activate() as collector:
                while True:
                    stream = agent.astream(
                        payload,
                        config=config,
                        stream_mode=["messages", "updates"],
                        subgraphs=True,
                    )
                    try:
                        # Dual mode + subgraphs => each item is (namespace, mode, data):
                        #   mode=="messages": data = (message_chunk, metadata)
                        #   mode=="updates" : data = {node_name: state_update}
                        async for namespace, mode, data in stream:
                            agent_path = _path(namespace)
                            if mode == "messages":
                                for ev in self._token_events(data, agent_path, streamed_ids):
                                    yield ev
                            elif mode == "updates":
                                if not isinstance(data, dict):
                                    continue
                                for _node_name, state in data.items():
                                    if not isinstance(state, dict):
                                        continue
                                    for msg in state.get("messages", []):
                                        for ev in _msg_to_events(msg, agent_path, streamed_ids):
                                            yield ev
                                    # Task planning: TodoListMiddleware writes the `todos`
                                    # state channel inside the same node update as messages.
                                    # Emit AFTER the messages so the timeline stays causal
                                    # (write_todos tool card precedes its list effect).
                                    todos = _normalize_todos(state.get("todos"))
                                    if todos is not None and todos != last_todos.get(agent_path):
                                        last_todos[agent_path] = todos
                                        yield SSEEvent(
                                            type="todos",
                                            agent_path=agent_path,
                                            payload={"todos": todos},
                                        )
                                # 文件变更徽标：backend 在工具落盘瞬间记入 collector，
                                # 本 updates 块的消息事件发完后 drain——线序恒为
                                # tool_call → tool_result → file_change，因果正确。
                                for ev in self._file_change_events(
                                    file_change_tracker.drain(collector), agent_path,
                                ):
                                    yield ev
                    finally:
                        # 确定性关闭 astream 生成器：中止/断连路径若仅弃置（悬挂在
                        # yield 处），GC 终结器的 athrow 遇 langgraph 内部吞掉
                        # GeneratorExit 后再 yield 的行为 → "async generator ignored
                        # GeneratorExit" 以未检索任务异常炸到事件循环日志（语音
                        # 中止/页面静置 GC 触发时复现）。清理路径就地吞残余异常，
                        # 不改写正在传播的主异常。
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await stream.aclose()

                    state = await agent.aget_state(config)

                    if state.next:
                        if _has_interrupt(state):
                            # 终 drain 兜底"节点完成后、interrupt 前"的最后一批写入
                            for ev in self._file_change_events(
                                file_change_tracker.drain(collector), agent_path,
                            ):
                                yield ev
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
                        # 终 drain 兜底"节点完成后、done 前"的最后一批写入
                        for ev in self._file_change_events(
                            file_change_tracker.drain(collector), agent_path,
                        ):
                            yield ev
                        yield SSEEvent(type="done", payload={})
                        return
        except Exception as e:
            logger.exception("stream error: %s", e)
            # Error floor: always close the stream with an error event so the
            # front-end can determine final state (plan §11.2.3). 先取走已落盘
            # 但未 drain 的变更（collector 引用仍有效），徽标不因流出错而丢失。
            for ev in self._file_change_events(
                file_change_tracker.drain(collector), agent_path,
            ):
                yield ev
            yield SSEEvent(type="error", payload={"message": str(e)})

    @staticmethod
    def _file_change_events(
        records: list[file_change_tracker.FileChangeRecord],
        agent_path: str,
    ) -> list[SSEEvent]:
        """把一批 FileChangeRecord 包装为单个 file_change SSE 事件（空批→无事件）。"""
        if not records:
            return []
        return [
            SSEEvent(
                type="file_change",
                agent_path=agent_path,
                payload={"records": [r.to_payload() for r in records]},
            ),
        ]

    @staticmethod
    def _token_events(
        data,
        agent_path: str,
        streamed_ids: set[str],
    ) -> list[SSEEvent]:
        """Turn one messages-mode chunk (chunk, metadata) into token events.

        Only AI text/reasoning streams token-by-token; ToolMessage / HumanMessage
        chunks are ignored here (tool results are emitted atomically via updates).
        """
        if not isinstance(data, tuple) or len(data) != 2:
            return []
        chunk, meta = data
        if not isinstance(chunk, AIMessage):
            return []
        meta = meta if isinstance(meta, dict) else {}
        sid = _stream_id(chunk, meta, agent_path)

        events: list[SSEEvent] = []
        reasoning = _reasoning_delta(chunk)
        if reasoning:
            streamed_ids.add(sid)
            events.append(SSEEvent(
                type="token",
                agent_path=agent_path,
                payload={"id": sid, "delta": reasoning, "reasoning": True},
            ))
        content = _text_delta(getattr(chunk, "content", None))
        if content:
            streamed_ids.add(sid)
            events.append(SSEEvent(
                type="token",
                agent_path=agent_path,
                payload={"id": sid, "delta": content, "reasoning": False},
            ))
        return events
