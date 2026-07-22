/* MCP + Skills 类型 —— 对应后端 models/schemas.py (McpServerCreate/McpServerOut/
   ToolInfo/SkillInfo) + api/mcp.py + api/skills.py。
   工具名命名空间 {server}__{tool}（防跨 server 冲突）。 */
import type { ISODateTime, UUID } from './common'

export type McpKind = 'stdio' | 'http'

export interface McpServerCreate {
  name: string
  kind: McpKind
  config: Record<string, unknown> // stdio: {command,args,env} | http: {url,headers}
  enabled: boolean
}

export interface McpServerOut {
  id: UUID
  name: string
  kind: string
  config_json: Record<string, unknown>
  enabled: boolean
  created_at: ISODateTime
}

/** 全量命名空间工具（agent 编辑器多选下拉用，api/mcp.py list_tools） */
export interface ToolInfo {
  name: string // 形如 "UnifiedGateway__web_search"
  description: string
}

/** GET /mcp/status 返回（mcp_lifecycle.health_check，形状宽松） */
export interface McpStatus {
  healthy?: boolean
  server_count?: number
  tool_count?: number
  servers?: Array<{
    name: string
    healthy: boolean
    tool_count: number
  }>
  [k: string]: unknown
}

/** 技能（schemas.py SkillInfo） */
export interface SkillInfo {
  name: string
  file_count: number
}
