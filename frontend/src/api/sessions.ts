/* 会话 API —— 对应后端 api/sessions.py（prefix /sessions）。
   CRUD + checkpointer 历史。流式对话见 stream.ts。 */
import { del, get, post } from './client'
import type { MessageOut, SessionCreate, SessionOut, UUID } from '@/types'

export const sessionsApi = {
  create: (body: SessionCreate) => post<SessionOut>('/sessions', body),

  /** 列出用户会话（含 agent_name） */
  listByUser: (userId: string) => get<SessionOut[]>('/sessions', { user_id: userId }),

  get: (id: UUID) => get<SessionOut>(`/sessions/${id}`),

  /** 归档会话 */
  archive: (id: UUID) => del<SessionOut>(`/sessions/${id}`),

  /** checkpointer 历史消息（空数组非错误，plan §11.1） */
  history: (id: UUID) => get<MessageOut[]>(`/sessions/${id}/history`),
}
