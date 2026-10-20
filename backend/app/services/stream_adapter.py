"""astream -> SSE event conversion (plan §5.2).

Streams in two modes simultaneously to deliver **token-level** output:
- stream_mode="messages" : LLM token deltas  -> `token` events (live content + reasoning)
- stream_mode="updates"  : node-complete msgs -> `tool_call` / `tool_result`
  (and `thinking` / `agent_message` as an atomic fallback for any AI message
  that was NOT streamed token-by-token).
  Node updates carrying a `todos` state change (TodoListMiddleware, per-node
  task planning) additionally emit a `todos` event — deduped per agent_path.
  Every node-complete AIMessage carrying `usage_metadata` (model client must
  run with stream_usage=True, see model_store) additionally emits a `usage`
  event — deduped by message id across the dual stream modes, one per LLM
  call; the front-end accumulates these into session-level token totals and
  stream.py/voice.py mirror each increment into session.usage_totals.

Dedup: every AI message delivered as tokens is recorded in `streamed_ids`
(matched by message id). In updates mode its content/reasoning are skipped to
avoid double-emitting, while its tool_calls still emit (those are not streamed).
This keeps the existing event contract intact — `tool_call` / `tool_result` /
`interrupt` / `done` / `error` behave exactly as before; `token` is the only
new event, and content/reasoning now flow through it when the provider streams.

HITL origins (declarative interrupt_on vs interrupt() primitive) are unified at
the SSE layer: both surface as `event: interrupt`, front-end does not
differentiate (plan §5.3).

Truncation guard (fail-visible): an END-routed final AI message that carries no
`finish_reason` means the provider stream was cut before the terminal chunk
(the SDK ends the stream silently instead of raising). The run then closes with
`error` instead of `done`, so a half-generated turn is never recorded as
successful (chat: banner + manual resend; cron: existing failure accounting
and next-slot rerun). See `_truncated_final_message`.

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
from collections.abc import AsyncIterator
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core import file_change_tracker
from app.services.thinking_patch import extract_reasoning
from app.services.tool_content import content_to_str
from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)


def _path(namespace: tuple) -> str:
    return "/".join(namespace) if namespace else "root"


def _content_str(content: Any) -> str:
    # 块列表（MCP wire 形态）提取 text 拼接，避免 str() 产出 repr（详见模块注释）
    return content_to_str(content)


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


def _normalize_todos(raw: Any) -> list[dict] | None:
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

        reasoning = extract_reasoning(msg)
        if reasoning and not already_streamed:
            events.append(SSEEvent(
                type="thinking",
                agent_path=agent_path,
                # id：产生该段的 AI 消息 id，供前端按消息去重（契约纯增量）
                payload={"content": reasoning, "id": msg.id},
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
                # id 同 thinking：非流式消息的唯一标识，回放场景去重依据
                payload={"content": _content_str(msg.content), "name": msg.name, "id": msg.id},
            ))
    elif isinstance(msg, ToolMessage):
        payload = {
            "tool_call_id": msg.tool_call_id,
            "content": _content_str(msg.content),
            "name": msg.name,
        }
        # jev kb 门控观测标记（契约纯增量：仅门控显式注入时出现）。UI 据此显示
        # 「jev 未介入」徽标；response_metadata 不进模型请求，LLM 不可见
        jev_kb = (getattr(msg, "response_metadata", None) or {}).get("jev_kb")
        if isinstance(jev_kb, str):
            payload["jev_kb"] = jev_kb
        events.append(SSEEvent(
            type="tool_result",
            agent_path=agent_path,
            payload=payload,
        ))
    # HumanMessage and other types are not forwarded (user input already known)
    return events


def has_interrupt(state) -> bool:
    """True if the graph paused on a HITL interrupt.

    与 state.next 非空区分：中止/崩溃 run 残留的孤儿 pending 同样使
    state.next 非空，但 tasks 不带 interrupts——取消收尾（cancel_run）与
    send 门禁（_refuse_or_heal_pending）据此区分"真中断"与"孤儿 pending"。
    """
    for task in getattr(state, "tasks", []):
        if getattr(task, "interrupts", None):
            return True
    return False


def _usage_from_message(msg) -> dict | None:
    """Normalize a message's usage_metadata into the fixed five-key shape.

    Source keys vary across langchain-core versions / providers:
    - input cache hit: input_token_details['cache_read'] (newer) or
      ['cached_tokens'] (older) — both accepted;
    - output reasoning: output_token_details['reasoning'].
    Returns None when the message carries no usage (client ran without
    stream_usage, provider didn't return usage, or not an AI message).
    """
    um = getattr(msg, "usage_metadata", None)
    if not isinstance(um, dict):
        return None
    itd = um.get("input_token_details") or {}
    otd = um.get("output_token_details") or {}
    input_tokens = um.get("input_tokens") or 0
    output_tokens = um.get("output_tokens") or 0
    return {
        "input_tokens": input_tokens,
        "cached_tokens": itd.get("cache_read") or itd.get("cached_tokens") or 0,
        "output_tokens": output_tokens,
        "reasoning_tokens": otd.get("reasoning") or 0,
        "total_tokens": um.get("total_tokens") or (input_tokens + output_tokens),
    }


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


# 终末回合截断时下发的 error 文案（fail-visible）。聊天侧呈横幅、用户重发；
# cron 侧写入 cron_run.error、按既有失败语义于下个槽位自动重跑。
_TRUNCATED_FINAL_MSG = "模型响应流被截断（未收到结束帧），本轮未产出最终答复，请重试"

# 截断类错误的机器可读标记（error payload.code）：cron 调度器据此识别并整轮
# 自动重试（见 cron_scheduler._invoke）；聊天侧只按 message 呈横幅，忽略 code。
STREAM_TRUNCATED_CODE = "stream_truncated"


def _truncated_final_message(state) -> Optional[AIMessage]:
    """识别"静默截断"的终末回合（END 路由的最终答复缺 finish_reason）。

    判据：OpenAI 兼容协议中 finish_reason 是完成响应的必备字段——流式下它随
    终止 chunk 到达并合入聚合消息的 response_metadata（非流式同理）。终末
    AI 消息（无 tool_calls，即被路由到 END 的"最终答复"）缺它，唯一解释是
    provider 流在中途被掐断、终止 chunk 从未到达：openai SDK 对这种"连接
    干净关闭"不抛异常，聚合出的消息 content 常为空、仅剩半截 reasoning。
    若照常发 done，残缺轮次会被
    记为成功——cron 记 ok、聊天侧无任何提示，历史只剩"思考过程"块。

    返回截断的终末消息（供调用方记日志），无截断迹象返回 None。不判定的
    形态一律放行（维持 done，宁漏勿误）：
    - 尾消息带 tool_calls：工具调用轮由 tools 节点续跑，缺 finish_reason
      无碍（下一次模型调用自然重建）；
    - 尾消息非 AI / 消息为空：异常形态，判据不适用。
    """
    messages = (getattr(state, "values", None) or {}).get("messages") or []
    if not messages:
        return None
    final = messages[-1]
    if not isinstance(final, AIMessage) or final.tool_calls:
        return None
    finish = (getattr(final, "response_metadata", None) or {}).get("finish_reason")
    if finish:
        return None
    return final


class StreamAdapter:
    """Convert agent.astream(...) into an SSE event stream."""

    def __init__(self, max_invoke_retries: int = 2):
        self.max_invoke_retries = max_invoke_retries

    async def stream_message(
        self,
        agent: CompiledStateGraph,
        content: str | list,
        config: dict,
        rubric: str | None = None,
        upload_ids: list[str] | None = None,
        raw_content: str | None = None,
        kb_ids: list[str] | None = None,
        agent_id: str | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """Stream a fresh user message. Yields until done / interrupt / error.

        content may be a plain str (text) or a multimodal list (text + image_url
        blocks) assembled by ContentComposer.

        upload_ids / raw_content / kb_ids / agent_id are stashed on the message's
        additional_kwargs so the checkpointer persists them alongside content
        (they never enter the model payload — the model only sees ``content``):
        - upload_ids: recovered on /history to render attachment thumbnails
          (read directly, never parsed out of content).
        - raw_content: the user's original input. Whenever ContentComposer
          rewrote content (attachment attribute table injected, KB anchor
          table injected) we keep the original text so history shows the
          user's real message instead of the internal prompt scaffold.
          Stored unconditionally — a bare-KB message has its content rewritten
          too, and a bare-image message's content is a multimodal list.
        - kb_ids: the knowledge bases selected for this message. Recovered on
          /history to re-render the KB badges on the user bubble, and on
          withdraw to prefill the composer's KB selector.
        - agent_id: 锚点回合归属（会话-智能体解耦，plan 1790660700000 §4.4）。
          withdraw 清理 `_summarization_event` 旁通道时，若当前解析图无该
          通道，经它取"产出该回合的图"执行（通道盲区的解法）。
        """
        additional_kwargs: dict[str, Any] = {}
        if upload_ids:
            additional_kwargs["upload_ids"] = upload_ids
        if kb_ids:
            additional_kwargs["kb_ids"] = kb_ids
        if agent_id:
            additional_kwargs["agent_id"] = agent_id
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
        # ids of AI messages whose usage was already emitted as a `usage` event
        # (dedup: the dual stream modes may both surface the same message)
        usage_counted: set[str] = set()
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
                                        for ev in self._usage_events(
                                            msg, agent_path, usage_counted
                                        ):
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
                        # GeneratorExit" 以未检索任务异常炸到事件循环日志。清理路径
                        # 就地吞残余异常，不改写正在传播的主异常。
                        with contextlib.suppress(Exception, asyncio.CancelledError):
                            await stream.aclose()

                    state = await agent.aget_state(config)

                    if state.next:
                        if has_interrupt(state):
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
                        # 截断守卫（fail-visible）：终末答复缺 finish_reason =
                        # 流在终止 chunk 到达前被掐断，残缺轮次不得记为
                        # 成功——改发 error（见 _truncated_final_message）。
                        truncated = _truncated_final_message(state)
                        if truncated is not None:
                            logger.warning(
                                "final turn truncated (msg id=%s): END-routed AI "
                                "message carries no finish_reason, emitting error "
                                "instead of done",
                                getattr(truncated, "id", None),
                            )
                            yield SSEEvent(
                                type="error",
                                payload={
                                    "message": _TRUNCATED_FINAL_MSG,
                                    "code": STREAM_TRUNCATED_CODE,
                                },
                            )
                            return
                        yield SSEEvent(type="done", payload={})
                        return
        except Exception as e:
            if isinstance(e.__context__, GeneratorExit):
                # 关闭途中（aclose/asyncgen 终结器）抛出的清理异常：此时生成器
                # 正在处理 GeneratorExit，再 yield 是非法的（"async generator
                # ignored GeneratorExit"）。原样上抛交给关闭方，不走 error floor。
                raise
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
    def _usage_events(
        msg,
        agent_path: str,
        counted: set[str],
    ) -> list[SSEEvent]:
        """Turn one node-complete message into 0..1 `usage` SSE events.

        Emitted once per LLM call (per AI message): only updates-mode complete
        messages are inspected — the messages-mode final usage chunk shares the
        same message id, so counting here with an id-keyed dedup set covers
        both modes exactly once. Messages without an id (rare; langchain
        auto-ids generated AI messages) are counted without dedup.
        """
        usage = _usage_from_message(msg)
        if usage is None:
            return []
        mid = getattr(msg, "id", None)
        if mid:
            key = str(mid)
            if key in counted:
                return []
            counted.add(key)
        return [
            SSEEvent(
                type="usage",
                agent_path=agent_path,
                payload={"id": mid, **usage},
            )
        ]

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
