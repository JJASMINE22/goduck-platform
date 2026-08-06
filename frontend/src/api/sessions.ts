/* 会话 API —— 对应后端 api/sessions.py（prefix /sessions）。
   CRUD + checkpointer 历史。流式对话见 stream.ts。 */
import { del, get, post } from './client'
import type { MessageOut, SessionCreate, SessionOut, UUID, WithdrawRequest, WithdrawResponse } from '@/types'

export const sessionsApi = {
  create: (body: SessionCreate) => post<SessionOut>('/sessions', body),

  /** 列出用户会话（含 agent_name） */
  listByUser: (userId: string, includeArchived = false) =>
    get<SessionOut[]>('/sessions', { user_id: userId, include_archived: includeArchived }),

  get: (id: UUID) => get<SessionOut>(`/sessions/${id}`),

  /** 归档会话 */
  archive: (id: UUID) => del<SessionOut>(`/sessions/${id}`),

  /** 彻底删除（物理删 + 清 checkpoint，不可恢复）→ 204 */
  purge: (id: UUID) => post<void>(`/sessions/${id}/purge`),

  /** checkpointer 历史消息（空数组非错误，plan §11.1） */
  history: (id: UUID) => get<MessageOut[]>(`/sessions/${id}/history`),

  /** 回撤一个回合（软删）→ 返回原文 + 附件用于回填发送框；流式中后端 409 */
  withdraw: (id: UUID, body: WithdrawRequest) =>
    post<WithdrawResponse>(`/sessions/${id}/withdraw`, body),
}
