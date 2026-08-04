/* 会话类型 —— 对应后端 models/schemas.py (SessionCreate/SessionOut/MessageOut) +
   api/sessions.py。thread_id 是 LangGraph checkpointer 的键。 */
import type { ISODateTime, UUID } from './common'
import type { UploadOut } from './upload'

export interface SessionCreate {
  user_id: string
  agent_id: UUID
  title?: string | null
  thread_id?: string | null // 自定义；省略则后端自动生成
}

export type SessionStatus = 'active' | 'archived'

export interface SessionOut {
  id: UUID
  agent_id: UUID
  user_id: string
  thread_id: string
  title?: string | null
  status: SessionStatus
  created_at: ISODateTime
  updated_at: ISODateTime
  agent_name?: string | null
}

/** LangGraph checkpointer 历史消息（schemas.py MessageOut） */
export interface MessageOut {
  id?: string | null
  role: string // "human" | "ai" | "tool" | ...
  name?: string | null
  content: string
  tool_calls?: Array<Record<string, unknown>> | null
  tool_call_id?: string | null
  // 仅 human 消息：来自消息 additional_kwargs（发送时写入）→ 历史接口批量查 upload 表回填
  attachments?: UploadOut[]
}
