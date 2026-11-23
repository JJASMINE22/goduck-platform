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
    # —— token 用量（每条 AI 消息一次：input/cached/output/reasoning/total 五键，
    #    前端会话级累加展示，stream.py/voice.py 同步入库 session.usage_totals）——
    "usage",
    # —— 附件解析进度（非错误型；前端映射临时徽标，首个 token 自然顶替）——
    "status",
    # —— 排水回合首帧（queued-send：携带队列条目展示元数据，前端据此把条目
    #    从待发条带物化为时间线用户气泡；仅 QueueService 排水启动的 run 产出，
    #    直发路径的气泡由前端乐观渲染）——
    "turn_start",
    "interrupt",
    "done",
    "error",
    # —— run 显式取消的终态（POST /cancel、语音 cancel 帧）；前端与 done/error
    #    同列为终止事件，落态语义=手动中止（固化半截 + 「用户已手动中断」）——
    "canceled",
    # —— 观察者重挂失败信号（落后超出缓冲 / run 已不存在）：前端回落
    #    /history + /interrupt 重建后重新挂观察流 ——
    "resync",
    # —— 语音 STT（仅 WS 下行隧道使用，SSE 端点不产出）——
    "stt_chunk",   # 部分 Turn：composer 实时渲染中间稿
    "stt_output",  # 合并稿（草稿+终稿）Turn：触发会话推理（镜像 POST /messages）
    "stt_empty",   # flush 后判空：复位麦克风，不产生消息（草稿保留）
]


class SSEEvent(BaseModel):
    type: EventType
    agent_path: Optional[str] = None  # "root" | "root/researcher" | ...
    payload: dict[str, Any] = {}

    def to_sse(self, event_id: Optional[str] = None) -> dict:
        """Format for sse-starlette EventSourceResponse (comment=False by default).

        event_id 为 SSE 规范的 ``id:`` 字段（RunManager 注入 ``{run_token}.{seq}``），
        浏览器 EventSource 重连时自动以 Last-Event-ID 回带，服务端据此增量回放。
        """
        out = {"event": self.type, "data": self.model_dump_json(exclude_none=True)}
        if event_id:
            out["id"] = event_id
        return out
