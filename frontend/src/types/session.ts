/* 会话类型 —— 对应后端 models/schemas.py (SessionCreate/SessionOut/MessageOut) +
   api/sessions.py。thread_id 是 LangGraph checkpointer 的键。 */
import type { ISODateTime, UUID } from './common'
import type { UploadOut } from './upload'
import type { UsageTotals } from './stream'

export interface SessionCreate {
  user_id: string
  /** 可选（会话-智能体解耦）：不选则创建空会话，首条消息发送时才确定智能体 */
  agent_id?: UUID | null
  title?: string | null
  thread_id?: string | null // 自定义；省略则后端自动生成
}

export type SessionStatus = 'active' | 'archived'

export interface SessionOut {
  id: UUID
  /** 最近使用的智能体（会话-智能体解耦）：send 时写入。null=空会话尚未选择，
   *  或上次智能体已被删除（FK SET NULL）。composer 选择器据此初始化默认选中 */
  last_agent_id?: UUID | null
  user_id: string
  thread_id: string
  title?: string | null
  status: SessionStatus
  /** 会话类型：'chat'=普通会话 | 'cron'=定时任务专用（只读 + HITL 审批） */
  kind?: 'chat' | 'cron'
  created_at: ISODateTime
  updated_at: ISODateTime
  /** 最近使用智能体的名称（LEFT JOIN；agent 已删/未选择时为 null） */
  agent_name?: string | null
  /** 会话级 token 用量累计（null/缺省 = 尚无消耗；流中由 usage 事件前端累加） */
  usage_totals?: UsageTotals | null
}

/** 知识库最小引用（schemas.py KbRef）：徽标渲染与回撤回填所需的全部信息 */
export interface KbRef {
  id: UUID
  name: string
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
  // 仅 human 消息：本条消息启用的知识库（kb_ids 同源，批量解析为 id+名称），
  // 用户气泡据此渲染 KB 徽标——与 content（已注入锚点脚手架）解耦
  kbs?: KbRef[]
  // 仅 ai 消息：思考内容（additional_kwargs.reasoning_content，随 checkpoint 持久化），
  // 历史回放据此重建"思考过程"折叠块——与实时流 ThinkingBlock 同构
  reasoning?: string | null
}

/** 回撤一个会话回合（schemas.py WithdrawRequest / WithdrawResponse） */
export interface WithdrawRequest {
  message_id: string // LangGraph 用户消息 id（定位被回撤的整段回合）
  /** 弱绑定取图（§4.4）：显式传入优先于 last_agent_id——last 悬空（agent 已删）
   *  时"仅选中、不发送"即可恢复回撤；缺省回落 */
  agent_id?: UUID | null
}

export interface WithdrawResponse {
  raw_content: string // 被回撤用户消息原文（回填发送框）
  attachments: UploadOut[] // 被回撤用户消息的附件（回填发送框 chip）
  kbs: KbRef[] // 被回撤用户消息启用的知识库（回填发送框 KB 选择器）
  removed_ids: string[] // 实际软删的消息 id 列表
}
