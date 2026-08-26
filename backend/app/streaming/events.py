"""SSE event type definitions.

StreamAdapter centralizes all "message -> SSE event" mapping here.
Frontend consumes a uniform event stream regardless of HITL origin
(declarative interrupt_on vs interrupt() primitive — unified at SSE layer).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel

EventType = Literal[
    "token",
    "tool_call",
    "tool_result",
    "thinking",
    "agent_message",
    "todos",
    "file_change",
    # —— 附件解析进度（非错误型；前端映射临时徽标，首个 token 自然顶替）——
    "status",
    "interrupt",
    "done",
    "error",
    # —— 语音 STT（仅 WS 下行隧道使用，SSE 端点不产出）——
    "stt_chunk",   # 部分 Turn：composer 实时渲染中间稿
    "stt_output",  # 合并稿（草稿+终稿）Turn：触发会话推理（镜像 POST /messages）
    "stt_empty",   # flush 后判空：复位麦克风，不产生消息（草稿保留）
]


class SSEEvent(BaseModel):
    type: EventType
    agent_path: Optional[str] = None  # "root" | "root/researcher" | ...
    payload: dict[str, Any] = {}

    def to_sse(self) -> dict:
        """Format for sse-starlette EventSourceResponse (comment=False by default)."""
        return {"event": self.type, "data": self.model_dump_json(exclude_none=True)}
