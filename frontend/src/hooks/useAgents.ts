/* 智能体 hooks —— 对应 api/agents.ts。
   变更后失效 ["agents"] + 详情 + 该 agent 历史。
   ⚠️ update/restore 会级联删该 agent 全部会话（后端语义）→ 同时失效 sessions。
   validate 不落库，故用 mutation 不失效缓存（编辑器实时预览）。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentsApi } from '@/api/agents'
import { toastSuccess } from '@/lib/antdStatic'
import type {
  AgentCreate,
  AgentUpdate,
  RestoreRequest,
  UUID,
  ValidateRequest,
} from '@/types'
import { queryKeys } from './queryKeys'

export function useAgents(includeArchived = false) {
  return useQuery({
    queryKey: [...queryKeys.agents, { includeArchived }],
    queryFn: () => agentsApi.list(includeArchived),
  })
}

export function useAgent(id: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.agent(id ?? ''),
    queryFn: () => agentsApi.get(id!),
    enabled: !!id,
  })
}

export function useAgentHistory(id: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.agentHistory(id ?? ''),
    queryFn: () => agentsApi.history(id!),
    enabled: !!id,
  })
}

/** 结构化拓扑（topology.json）。enabled 控制按需拉取，避免列表 N+1。
 *  staleTime 复用全局 30s，悬浮预览跨行复用缓存。 */
export function useAgentTopology(id: UUID | null | undefined, enabled = true) {
  return useQuery({
    queryKey: queryKeys.agentTopology(id ?? ''),
    queryFn: () => agentsApi.topologyJson(id!),
    enabled: !!id && enabled,
  })
}

/** 校验 + mermaid 预览（不落库）。编辑器实时预览调用，由组件做防抖。 */
export function useValidateAgent() {
  return useMutation({ mutationFn: (body: ValidateRequest) => agentsApi.validate(body) })
}

function invalidateAgent(qc: ReturnType<typeof useQueryClient>, id?: UUID) {
  const tasks: Promise<unknown>[] = [
    qc.invalidateQueries({ queryKey: queryKeys.agents }),
    qc.invalidateQueries({ queryKey: ['sessions'] }), // 更新会级联删会话
  ]
  if (id) {
    tasks.push(qc.invalidateQueries({ queryKey: queryKeys.agent(id) }))
    tasks.push(qc.invalidateQueries({ queryKey: queryKeys.agentHistory(id) }))
  }
  return Promise.all(tasks)
}

export function useCreateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: AgentCreate) => agentsApi.create(body),
    onSuccess: async (detail) => {
      await invalidateAgent(qc, detail.id)
      toastSuccess('智能体已创建')
    },
  })
}

export function useUpdateAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: AgentUpdate }) => agentsApi.update(id, body),
    onSuccess: async (detail) => {
      await invalidateAgent(qc, detail.id)
      toastSuccess('智能体已更新（旧会话已清除）')
    },
  })
}

export function useRestoreAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: RestoreRequest }) =>
      agentsApi.restore(id, body),
    onSuccess: async (detail) => {
      await invalidateAgent(qc, detail.id)
      toastSuccess('已还原到历史版本')
    },
  })
}

export function useArchiveAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => agentsApi.archive(id),
    onSuccess: async (agent) => {
      await invalidateAgent(qc, agent.id)
      toastSuccess('智能体已归档')
    },
  })
}

export function usePurgeAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => agentsApi.purge(id),
    onSuccess: async () => {
      await invalidateAgent(qc)
      toastSuccess('智能体已彻底删除')
    },
  })
}
