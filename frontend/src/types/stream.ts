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
  | 'todos'
  | 'file_change'
  | 'usage'
  | 'status'
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

// —— status payload（附件预消费进度，非错误型；映射临时徽标，首个 token 顶替）——
export interface StatusPayload {
  /** parsing=正在解析附件；skipped=单附件解析失败已跳过（降级发送）；parsed=全部解析完成 */
  phase: 'parsing' | 'parsed' | 'skipped'
  /** parsing：本批文件名 */
  files?: string[]
  /** skipped：失败文件名 + 原因 */
  file?: string
  reason?: string
  /** parsed：成功数量 */
  count?: number
}

// —— todos payload（stream_adapter._normalize_todos：TodoListMiddleware 状态通道）——
/** 单条任务项；status 与后端 write_todos 对齐 */
export interface TodoItem {
  content: string
  status: 'pending' | 'in_progress' | 'completed' | string
}

/** todos 事件 payload：该 agent_path 的全量清单（增量由前端整表替换） */
export interface TodosPayload {
  todos: TodoItem[]
}

// —— file_change payload（core/file_change_tracker.py FileChangeRecord）——
/** 单个文件的行级差异摘要；added/removed 为 null 表示二进制/超大/目录（只显动作） */
export interface FileChangeRecord {
  /** 相对用户工作区根的路径（与 WorkspaceNode.path 同源，可直接匹配树节点） */
  path: string
  action: 'create' | 'edit' | 'delete'
  added: number | null
  removed: number | null
}

/** file_change 事件 payload：一批（通常一个工具节点）落盘记录 */
export interface FileChangePayload {
  records: FileChangeRecord[]
}

// —— usage payload（stream_adapter._usage_from_message：一次 LLM 调用的用量）——
export interface UsagePayload {
  /** 产生本次用量的 AI 消息 id（无 id 消息为 null） */
  id?: string | null
  input_tokens: number
  /** 输入中的缓存命中部分（input_tokens 已含，展示时勿重复计入发送量） */
  cached_tokens: number
  output_tokens: number
  /** 输出中的思考 token（output_tokens 已含） */
  reasoning_tokens: number
  total_tokens: number
}

/** 会话级累计（session.usage_totals，与 UsagePayload 同五键、无 id） */
export type UsageTotals = Omit<UsagePayload, 'id'>

// —— interrupt payload（hitl_resolver._extract_interrupt）——
// deepagents 的 action_requests 为扁平结构：每个待审批工具调用 = { name, args }
// （与 HumanInTheLoopMiddleware 输出一致，非嵌套的 { action: {...} }）。
// review_configs 与 action_requests 同序一一对应，键为 action_name / allowed_decisions。
export interface ActionRequest {
  name: string
  args: Record<string, unknown>
  /** 审批请求说明（InterruptOnConfig.description 工厂产物；如 Jev 概率标注）。
   *  TypedDict 原样透传（stream_adapter._extract_interrupt 无过滤），契约纯增量。 */
  description?: string
}

/** deepagents review_configs[i]：单条 action_request 的审核约束 */
export interface ReviewConfig {
  action_name?: string
  allowed_decisions?: DecisionType[]
}

export interface InterruptPayload {
  action_requests: ActionRequest[]
  review_configs: ReviewConfig[]
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
  /** 本次消息启用的知识库 id（非空时后端注入 KB 锚点，强暗示 agent 调 kb_search） */
  knowledge_base_ids?: UUID[]
}

export interface ResumeRequest {
  decisions: Decision[]
}

/** 聊天状态机（DESIGN 准则 F3 终态驱动） */
export type ChatStreamStatus = 'idle' | 'streaming' | 'interrupted' | 'error'
