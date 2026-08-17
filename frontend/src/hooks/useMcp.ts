/* MCP 运维 hooks —— 对应 api/mcp.ts。
   server CRUD 触发后端 refresh → 失效 servers/tools/status 三键。
   tools 供 agent 编辑器多选用，单独键便于精细失效。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { mcpApi } from '@/api/mcp'
import { toastSuccess } from '@/lib/antdStatic'
import type { McpServerCreate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

/** 失效 MCP 三键（后端 refresh 后数据全变） */
function invalidateMcp(qc: ReturnType<typeof useQueryClient>) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: queryKeys.mcpServers }),
    qc.invalidateQueries({ queryKey: queryKeys.mcpTools }),
    qc.invalidateQueries({ queryKey: queryKeys.mcpStatus }),
  ])
}

export function useMcpServers() {
  return useQuery({ queryKey: queryKeys.mcpServers, queryFn: mcpApi.listServers })
}

export function useMcpTools() {
  // 30s 轮询：CRUD 保存后后端在后台重连（保存秒回），新 server 的工具
  // 经轮询自动出现，无需手动刷新；状态轮询见 useMcpStatus。
  return useQuery({
    queryKey: queryKeys.mcpTools,
    queryFn: mcpApi.tools,
    refetchInterval: 30_000,
  })
}

export function useMcpStatus() {
  return useQuery({ queryKey: queryKeys.mcpStatus, queryFn: mcpApi.status, refetchInterval: 60_000 })
}

export function useCreateMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: McpServerCreate) => mcpApi.createServer(body),
    onSuccess: async () => {
      await invalidateMcp(qc)
      toastSuccess('MCP server 已注册')
    },
  })
}

export function useUpdateMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: McpServerCreate }) =>
      mcpApi.updateServer(id, body),
    onSuccess: async () => {
      await invalidateMcp(qc)
      toastSuccess('MCP server 已更新')
    },
  })
}

export function useDeleteMcpServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => mcpApi.deleteServer(id),
    onSuccess: async () => {
      await invalidateMcp(qc)
      toastSuccess('MCP server 已删除')
    },
  })
}

export function useRefreshMcp() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => mcpApi.refresh(),
    onSuccess: async () => {
      await invalidateMcp(qc)
      toastSuccess('已强制重连')
    },
  })
}
