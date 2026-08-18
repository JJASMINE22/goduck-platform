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
    "interrupt",
    "done",
    "error",
]


class SSEEvent(BaseModel):
    type: EventType
    agent_path: Optional[str] = None  # "root" | "root/researcher" | ...
    payload: dict[str, Any] = {}

    def to_sse(self) -> dict:
        """Format for sse-starlette EventSourceResponse (comment=False by default)."""
        return {"event": self.type, "data": self.model_dump_json(exclude_none=True)}
