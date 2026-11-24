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
  // 尾缀 'graph' 区分旧 mermaid 文本缓存（负载形态不同，防串型）
  agentTopology: (id: string) => ['agents', id, 'topology', 'graph'] as const,
  // 会话/任务按登录用户作用域（身份由 cookie 推导，无需入 key 维度）
  sessions: ['sessions'] as const,
  session: (id: string) => ['sessions', id] as const,
  sessionHistory: (id: string) => ['sessions', id, 'history'] as const,
  mcpServers: ['mcp', 'servers'] as const,
  mcpTools: ['mcp', 'tools'] as const,
  mcpStatus: ['mcp', 'status'] as const,
  models: ['models'] as const,
  modelsVision: ['models', { vision: true }] as const,
  embeddings: ['embeddings'] as const,
  skills: ['skills'] as const,
  skillDescription: (name: string) => ['skills', name, 'description'] as const,
  knowledgeBases: ['knowledge-bases'] as const,
  kbParserEngine: ['knowledge-bases', 'parser-engine'] as const,
  kbDocuments: (kbId: string) => ['knowledge-bases', kbId, 'documents'] as const,
  // 能力查询键含 agentId 维度（会话-智能体解耦 §4.4）：切换选中智能体即
  // keyed refetch（无需手动 invalidate）；'' = 缺省回落 last_agent_id
  attachmentCapability: (sessionId: string, agentId?: string | null) =>
    ['sessions', sessionId, 'attachment-capability', agentId ?? ''] as const,
  kbCapability: (sessionId: string, agentId?: string | null) =>
    ['sessions', sessionId, 'kb-capability', agentId ?? ''] as const,
  workspaceCapability: (sessionId: string, agentId?: string | null) =>
    ['sessions', sessionId, 'workspace-capability', agentId ?? ''] as const,
  workspaceTree: (sessionId: string) => ['sessions', sessionId, 'workspace-tree'] as const,
  workspaceFile: (sessionId: string, path: string) =>
    ['sessions', sessionId, 'workspace-file', path] as const,
  workspacePreview: (sessionId: string, path: string) =>
    ['sessions', sessionId, 'workspace-preview', path] as const,
  shellCapability: (sessionId: string, agentId?: string | null) =>
    ['sessions', sessionId, 'shell-capability', agentId ?? ''] as const,
  rubricCapability: (sessionId: string, agentId?: string | null) =>
    ['sessions', sessionId, 'rubric-capability', agentId ?? ''] as const,
  cronJobs: ['cron', 'jobs'] as const,
  cronRuns: (jobId: string) => ['cron', 'jobs', jobId, 'runs'] as const,
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
