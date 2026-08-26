/* Query key 约定 + QueryClient 工厂（DESIGN §7.1）。
   拆出纯函数文件，避免与 React 组件混导出触发 react-refresh 规则。
   - 查询：失败不全局弹（组件自行展示 error 状态 + 重试）
   - 变更：失败全局弹 toast（保留后端中文 detail） */
import { QueryClient } from '@tanstack/react-query'
import { ApiError } from '@/api/client'
import { getStaticFns } from '@/lib/antdStatic'

export const queryKeys = {
  health: ['health'] as const,
  agents: ['agents'] as const,
  agent: (id: string) => ['agents', id] as const,
  agentHistory: (id: string) => ['agents', id, 'history'] as const,
  agentTopology: (id: string) => ['agents', id, 'topology'] as const,
  sessions: (userId: string) => ['sessions', { userId }] as const,
  session: (id: string) => ['sessions', id] as const,
  sessionHistory: (id: string) => ['sessions', id, 'history'] as const,
  mcpServers: ['mcp', 'servers'] as const,
  mcpTools: ['mcp', 'tools'] as const,
  mcpStatus: ['mcp', 'status'] as const,
  models: ['models'] as const,
  modelsVision: ['models', { vision: true }] as const,
  skills: ['skills'] as const,
  knowledgeBases: ['knowledge-bases'] as const,
  kbDocuments: (kbId: string) => ['knowledge-bases', kbId, 'documents'] as const,
  attachmentCapability: (sessionId: string) =>
    ['sessions', sessionId, 'attachment-capability'] as const,
  kbCapability: (sessionId: string) => ['sessions', sessionId, 'kb-capability'] as const,
  workspaceCapability: (sessionId: string) =>
    ['sessions', sessionId, 'workspace-capability'] as const,
  workspaceTree: (sessionId: string) => ['sessions', sessionId, 'workspace-tree'] as const,
  workspaceFile: (sessionId: string, path: string) =>
    ['sessions', sessionId, 'workspace-file', path] as const,
  workspacePreview: (sessionId: string, path: string) =>
    ['sessions', sessionId, 'workspace-preview', path] as const,
  shellCapability: (sessionId: string) =>
    ['sessions', sessionId, 'shell-capability'] as const,
} as const

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
      mutations: {
        onError: (err) => {
          const msg = err instanceof ApiError ? err.message : '操作失败，请重试'
          getStaticFns()?.message.error(msg)
        },
      },
    },
  })
}
