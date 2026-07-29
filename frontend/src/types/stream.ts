/* SSE 流式 + HITL 类型 —— 对应后端 streaming/events.py (SSEEvent/EventType) +
   models/schemas.py (SendMessageRequest/ResumeRequest/Decision) +
   services/hitl_resolver.py (_extract_interrupt 输出形状)。
   聊天状态机唯一终态来源（DESIGN 准则 F3）。 */
import type { UUID } from './common'

/** 后端 EventType 字面量（streaming/events.py） */
export type SSEEventType =
  | 'token'
  | 'tool_call'
  | 'tool_result'
  | 'thinking'
  | 'agent_message'
  | 'interrupt'
  | 'done'
  | 'error'

/** 终止事件（DESIGN 准则 F3：状态机只据此判终态） */
export const TERMINAL_EVENTS: readonly SSEEventType[] = ['done', 'interrupt', 'error'] as const

/** 通用 SSE 事件壳（streaming/events.py SSEEvent） */
export interface SSEEvent<P = Record<string, unknown>> {
  type: SSEEventType
  agent_path?: string | null // "root" | "root/researcher"
  payload: P
}

// —— 中间事件 payload 形状 ——
export interface AgentMessagePayload {
  content: string
  name?: string | null
}

export interface ThinkingPayload {
  content: string
}

/** token 事件 payload：逐 token 增量（后端 stream_adapter._token_events） */
export interface TokenPayload {
  /** 流式消息聚合键（同一条 LLM 消息的 content/reasoning 共享此 id） */
  id: string
  /** 本次增量文本片段 */
  delta: string
  /** true=思考过程增量；false=正文增量 */
  reasoning?: boolean
}

export interface ToolCallPayload {
  name: string
  args: Record<string, unknown>
  id?: string | null
  caller?: string | null
}

export interface ToolResultPayload {
  tool_call_id: string
  content: string
  name?: string | null
}

export interface ErrorPayload {
  message: string
}

// —— interrupt payload（hitl_resolver._extract_interrupt）——
export interface ActionRequest {
  action?: {
    name: string
    args: Record<string, unknown>
  }
}

export interface InterruptPayload {
  action_requests: ActionRequest[]
  review_configs: Array<Record<string, unknown>>
  raw: Array<Record<string, unknown>>
}

// —— resume 决策（schemas.py Decision / DecisionEditAction）——
export type DecisionType = 'approve' | 'edit' | 'reject' | 'respond'

export interface DecisionEditAction {
  name: string
  args: Record<string, unknown>
}

export interface Decision {
  type: DecisionType
  message?: string | null
  edited_action?: DecisionEditAction | null
}

// —— 请求体（schemas.py）——
export interface Attachment {
  upload_id: UUID // 后端自查 kind/mime，前端只传 id（准则 F6）
}

export interface SendMessageRequest {
  content: string
  rubric?: string | null // 单次 rubric 覆盖（FixedRubric 优先）
  attachments?: Attachment[]
  // —— KB 留口（kb-hybrid-access-plan，后端未上线前不传）——
  kb_enabled?: boolean
  kb_collections?: string[]
  kb_versions?: Record<string, string>
}

export interface ResumeRequest {
  decisions: Decision[]
}

/** 聊天状态机（DESIGN 准则 F3 终态驱动） */
export type ChatStreamStatus = 'idle' | 'streaming' | 'interrupted' | 'error'
