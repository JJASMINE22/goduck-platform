/* MCP 运维 API —— 对应后端 api/mcp.py（prefix /mcp）。
   server CRUD（触发 refresh）+ 全量工具列表 + 健康检查 + 强制重连。 */
import { del, get, post, put } from './client'
import type { McpServerCreate, McpServerOut, McpStatus, ToolInfo, UUID } from '@/types'

export const mcpApi = {
  /** 连接健康 + 工具数 */
  status: () => get<McpStatus>('/mcp/status'),

  /** 强制重连所有 server（运维） */
  refresh: () => post<{ ok: boolean; status: McpStatus }>('/mcp/refresh'),

  listServers: () => get<McpServerOut[]>('/mcp/servers'),

  /** 注册/更新（触发 refresh） */
  createServer: (body: McpServerCreate) => post<McpServerOut>('/mcp/servers', body),

  updateServer: (id: UUID, body: McpServerCreate) =>
    put<McpServerOut>(`/mcp/servers/${id}`, body),

  deleteServer: (id: UUID) => del<{ ok: boolean }>(`/mcp/servers/${id}`),

  /** 全量命名空间工具（agent 编辑器多选下拉用） */
  tools: () => get<ToolInfo[]>('/mcp/tools'),
}
