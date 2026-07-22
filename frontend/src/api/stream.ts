/* 流式对话 API —— 对应后端 api/stream.py（prefix /sessions）。
   POST /sessions/{id}/messages 与 /resume 均返回 SSE，共用同一事件协议。
   终止事件固定 done / interrupt / error（plan §11.1）。
   resume 若当前未在 interrupt 状态 → 后端 409。 */
import { streamPostSse } from '@/lib/sse'
import type { StreamHandlers, StreamTermination } from '@/lib/sse'
import type { ResumeRequest, SendMessageRequest, UUID } from '@/types'

export const streamApi = {
  /** 发送消息 → SSE 流（聊天状态机驱动） */
  sendMessage: (
    sessionId: UUID,
    body: SendMessageRequest,
    handlers: StreamHandlers,
    signal?: AbortSignal,
  ): Promise<StreamTermination> =>
    streamPostSse(`/sessions/${sessionId}/messages`, body, handlers, signal),

  /** HITL 决策恢复 → SSE 流（接到 interrupt 事件后用户决策再调） */
  resume: (
    sessionId: UUID,
    body: ResumeRequest,
    handlers: StreamHandlers,
    signal?: AbortSignal,
  ): Promise<StreamTermination> =>
    streamPostSse(`/sessions/${sessionId}/resume`, body, handlers, signal),
}
