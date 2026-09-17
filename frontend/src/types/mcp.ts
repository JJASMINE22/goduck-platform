/* MCP + Skills 类型 —— 对应后端 models/schemas.py (McpServerCreate/McpServerOut/
   ToolInfo/SkillInfo) + api/mcp.py + api/skills.py。
   工具名命名空间 {server}__{tool}（防跨 server 冲突）。 */
import type { ISODateTime, UUID } from './common'

export type McpKind = 'stdio' | 'http' | 'streamable_http'

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

/** 单个 MCP server 的连接状态（/mcp/status 的 servers 成员） */
export interface McpServerStatus {
  name: string
  healthy: boolean
  tool_count: number
  kind: string
  error?: string | null
}

/** GET /mcp/status 返回（mcp_lifecycle.health_check 聚合结构） */
export interface McpStatus {
  healthy: boolean
  server_count: number
  tool_count: number
  servers: McpServerStatus[]
}

/** 删除/改名被引用的 MCP server 时后端 409 detail（api/mcp.py delete/update） */
export interface McpInUseDetail {
  message: string
  referencing_agents: Array<{ id: string; name: string }>
}

/** 技能（schemas.py SkillInfo） */
export interface SkillInfo {
  name: string
  file_count: number
}

/** 技能描述（schemas.py SkillDetail，GET /skills/{name}/description 解析 SKILL.md frontmatter） */
export interface SkillDetail {
  name: string
  description: string | null
}
