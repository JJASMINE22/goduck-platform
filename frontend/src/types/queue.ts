/* 会话排队发送（queued send）类型 —— 对应后端 models/schemas.py QueuedMessageOut/QueueOut。
   sent 审计行不在契约内（GET /queue 不返回）。 */
import type { UUID } from './common'
import type { KbRef } from './session'
import type { UploadOut } from './upload'

export type QueueItemStatus = 'pending' | 'sending' | 'failed' | 'canceled'

/** 会话队列整体态：idle=无待发 | active=自动排水中 | paused=挂起待用户决策 */
export type QueueState = 'idle' | 'active' | 'paused'

export type QueuePausedReason = 'error' | 'withdrawn' | 'restart'

/** 队列条目：附件/KB 已由服务端解析为展示元数据（与 /history 回显同构，
    供排队气泡物化为正式用户消息与回撤锚定） */
export interface QueuedItem {
  id: UUID
  agent_id: UUID
  content: string
  rubric?: string | null
  attachments: UploadOut[]
  kbs: KbRef[]
  status: QueueItemStatus
  fail_reason?: string | null
  created_at: string
}

export interface QueueView {
  state: QueueState
  paused_reason: QueuePausedReason | null
  items: QueuedItem[]
}
