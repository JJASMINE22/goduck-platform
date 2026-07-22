/* 智能体类型 —— 与后端 core/schema_utils.py (AgentConfig/RubricConfig) +
   models/schemas.py (AgentCreate/AgentOut/AgentDetail/...) 1:1 对齐。
   AgentConfig 是配置树「唯一真相」（DESIGN 准则 F1）。 */
import type { ISODateTime, UUID } from './common'

/** 后端 RubricConfig（schema_utils.py）—— 节点级评分自评 */
export interface RubricConfig {
  content?: string | null
  fixed?: boolean
  max_iterations?: number // 1..20
  grader_tool_names?: string[]
}

/**
 * 后端 AgentConfig（schema_utils.py）—— 配置树，递归 subagents。
 * 主代理: is_main=true, name/description 为 null, 可设 memory/workspace
 * 子代理: is_main 省略/false, 必须设 name/description, 禁 memory/workspace
 */
export interface AgentConfig {
  name?: string | null
  description?: string | null
  system_prompt: string // 必填
  tool_names?: string[] // MCP 命名空间工具，源 GET /mcp/tools
  skills?: string[] | null // 形如 "/skills/<name>/"
  memory?: string[] | null // "/memories/<name>.md"，仅主代理
  workspace?: string | null // 单一目录名，仅主代理
  interrupt_on?: Record<string, boolean> | null
  is_main?: boolean
  is_dicted?: boolean
  subagents?: AgentConfig[] | null // ★ 递归
  rubric?: RubricConfig | null
  allow_attachments?: boolean
}

// —— 请求体（schemas.py）——
export interface AgentCreate {
  name: string
  description?: string | null
  config: AgentConfig // 后端字段名是 config（dict 语义），前端按 AgentConfig 构造
  model_id: UUID
  creator: string
}

export interface AgentUpdate {
  config: AgentConfig
  model_id: UUID
  description?: string | null
  updater: string
}

export interface ValidateRequest {
  config: AgentConfig
}

export interface RestoreRequest {
  history_id: UUID
  operator: string
}

// —— 响应体（schemas.py）——
export type AgentStatus = 'draft' | 'active' | 'archived'

export interface AgentOut {
  id: UUID
  name: string
  description?: string | null
  model_id?: UUID | null
  status: AgentStatus
  creator: string
  updater?: string | null
  created_at: ISODateTime
  updated_at: ISODateTime
}

export interface AgentDetail extends AgentOut {
  config_json: AgentConfig // 后端存 dict，前端按 AgentConfig 解析
  active_mermaid?: string | null
}

export interface AgentHistoryItem {
  id: UUID
  action: string // "create" | "update"
  operator: string
  created_at: ISODateTime
}

export interface ValidateResponse {
  valid: boolean
  errors?: string[] | null
  mermaid?: string | null
}
