"""回合事件源：组装（附件 / KB 预消费）→ StreamAdapter → SSEEvent 流。

POST /messages 与语音 WS 的 voice 回合共用同一事件源；RunManager 以 run task
驱动它，SSE / WS 只是订阅者。原先散在 stream.py event_gen 与 voice.py
_run_agent_round 的"assemble → adapter"拼装逻辑收口于此（单一真相源，两出口
行为逐帧一致）。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable

from app.services.message_content import (
    BuiltMessage,
    assemble_message_content_stream,
)
from app.streaming.events import SSEEvent


def chat_event_source(
    st,
    session: dict,
    agent,
    config: dict,
    *,
    agent_id,
    user_query: str,
    attachments: list,
    knowledge_base_ids: list,
    rubric: str | None = None,
    raw_content: str | None = None,
) -> Callable[[], AsyncIterator[SSEEvent]]:
    """构建回合事件源工厂（即 RunManager.start_run 的 factory 参数）。

    附件 / KB 的能力与归属校验由调用方在流外完成（422/403/404 尽早暴露）；
    本源内的 assemble 只做预消费与锚点注入（status 进度事件随流透传）。
    agent_id 为当回合智能体（会话-智能体解耦）：随 HumanMessage
    additional_kwargs 持久化为**锚点回合归属**——withdraw 清理摘要旁通道
    时经它取"产出该回合的图"（plan 1790660700000 §4.4）。
    """

    async def _source() -> AsyncIterator[SSEEvent]:
        built: BuiltMessage | None = None
        async for item in assemble_message_content_stream(
            st,
            session,
            agent_id=agent_id,
            user_query=user_query,
            attachments=attachments,
            knowledge_base_ids=knowledge_base_ids,
        ):
            if isinstance(item, SSEEvent):
                yield item  # status：正在解析附件… / 已跳过…
            else:
                built = item
        assert built is not None  # assemble 生成器必以 BuiltMessage 收尾

        gen = st.stream_adapter.stream_message(
            agent,
            built.content,
            config,
            rubric,
            upload_ids=built.upload_ids,
            raw_content=raw_content,
            kb_ids=[str(k) for k in knowledge_base_ids],
            agent_id=str(agent_id),
        )
        try:
            async for ev in gen:
                yield ev
        finally:
            # 确定性关闭 adapter 生成器链（run 取消/中止路径；与旧 SSE event_gen
            # 同语义：防 GC 终结器路径的 "async generator ignored GeneratorExit"）
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await gen.aclose()

    return _source
