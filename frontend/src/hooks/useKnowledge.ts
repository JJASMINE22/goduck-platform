/* 知识库 hooks —— 对应 api/knowledge.ts。
   - useKnowledgeBases：列表（KB 禁用时后端 503，交由组件展示禁用态）。
   - 文档列表：含 pending/indexing 时轮询（入库异步，直至全部终态停轮询）。
   - 变更后失效 KB 列表（doc_count/chunk_count 跟随）+ 当前库文档列表。
   - 检索调试为按需触发（不进缓存），供文档抽屉内的搜索面板使用。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { knowledgeApi } from '@/api/knowledge'
import { sessionsApi } from '@/api/sessions'
import { toastSuccess } from '@/lib/antdStatic'
import type { KnowledgeBaseCreate, KnowledgeBaseUpdate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useKnowledgeBases() {
  return useQuery({ queryKey: queryKeys.knowledgeBases, queryFn: knowledgeApi.list })
}

/** 会话级知识库能力：驱动会话框 KB 图标可见性（准则 F2 能力驱动 UI）。
 *  503（KB 全局未启用 / agent 未开启知识库能力）时不重试。 */
export function useKnowledgeCapability(sessionId: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.kbCapability(sessionId ?? ''),
    queryFn: () => sessionsApi.knowledgeCapability(sessionId!),
    enabled: !!sessionId,
    retry: false,
  })
}

/** 单库文档列表：存在 pending/indexing 文档时每 2s 轮询，全终态后自动停。
 *  retry:false 避免 503（KB 未启用）时反复重试刷屏。 */
export function useKbDocuments(kbId: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.kbDocuments(kbId ?? ''),
    queryFn: () => knowledgeApi.listDocuments(kbId!),
    enabled: !!kbId,
    retry: false,
    refetchInterval: (query) => {
      const docs = query.state.data
      if (!docs?.length) return false
      return docs.some((d) => d.status === 'pending' || d.status === 'indexing') ? 2000 : false
    },
  })
}

function invalidateKb(qc: ReturnType<typeof useQueryClient>, kbId?: UUID) {
  void qc.invalidateQueries({ queryKey: queryKeys.knowledgeBases })
  if (kbId) void qc.invalidateQueries({ queryKey: queryKeys.kbDocuments(kbId) })
}

export function useCreateKb() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: KnowledgeBaseCreate) => knowledgeApi.create(body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.knowledgeBases })
      toastSuccess('知识库已创建')
    },
  })
}

export function useUpdateKb() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: KnowledgeBaseUpdate }) =>
      knowledgeApi.update(id, body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.knowledgeBases })
      toastSuccess('知识库已更新')
    },
  })
}

export function useDeleteKb() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => knowledgeApi.remove(id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.knowledgeBases })
      toastSuccess('知识库已删除')
    },
  })
}

export function useUploadDocument(kbId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => knowledgeApi.uploadDocument(kbId, file),
    onSuccess: () => invalidateKb(qc, kbId),
  })
}

export function useDeleteDocument(kbId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (docId: UUID) => knowledgeApi.removeDocument(kbId, docId),
    onSuccess: () => invalidateKb(qc, kbId),
  })
}

export function useReindexDocument(kbId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (docId: UUID) => knowledgeApi.reindexDocument(kbId, docId),
    onSuccess: () => invalidateKb(qc, kbId),
  })
}

/** 检索调试（按需触发，不缓存）。409=维度漂移，detail 直接展示。 */
export function useKbSearch(kbId: UUID) {
  return useMutation({
    mutationFn: ({ q, k }: { q: string; k: number }) => knowledgeApi.search(kbId, q, k),
  })
}
