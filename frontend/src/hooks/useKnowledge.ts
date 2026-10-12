/* 知识库 hooks —— 对应 api/knowledge.ts。
   - useKnowledgeBases：列表。按全局能力开关门控（kb_enabled=false 时后端路由
     未挂载、接口 404，故 enabled=false 不发请求），交由组件展示禁用态。
   - 上传仅注册（pending=待解析）；解析手动触发（useParseDocument）。
   - 文档列表：含 pending/indexing 时轮询（解析异步，直至全部终态停轮询）。
   - 变更后失效 KB 列表（doc_count/chunk_count 跟随）+ 当前库文档列表。
   - 检索调试为按需触发（不进缓存），供文档抽屉内的搜索面板使用。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { knowledgeApi } from '@/api/knowledge'
import { sessionsApi } from '@/api/sessions'
import { toastSuccess } from '@/lib/antdStatic'
import type { KbParseRequest, KnowledgeBaseCreate, KnowledgeBaseUpdate, UUID } from '@/types'
import { useCapabilities } from './useCapabilities'
import { queryKeys } from './queryKeys'

/** KB 列表：仅当后端 kb_enabled=true 时启用（禁用时路由未挂载，避免 404 请求）。 */
export function useKnowledgeBases() {
  const { kb_enabled } = useCapabilities()
  return useQuery({
    queryKey: queryKeys.knowledgeBases,
    queryFn: knowledgeApi.list,
    enabled: kb_enabled,
    retry: false,
  })
}

/** 会话级知识库能力：驱动会话框 KB 图标可见性（准则 F2 能力驱动 UI）。
 *  allow_knowledge=false（KB 全局未启用 / agent 未开启知识库能力）时不重试。
 *  agentId=当前选中智能体（弱绑定解析，query key 含 agentId 随切换 refetch）。 */
export function useKnowledgeCapability(
  sessionId: UUID | null | undefined,
  agentId?: UUID | null,
) {
  return useQuery({
    queryKey: queryKeys.kbCapability(sessionId ?? '', agentId ?? null),
    queryFn: () => sessionsApi.knowledgeCapability(sessionId!, agentId ?? null),
    enabled: !!sessionId,
    retry: false,
  })
}

/** 解析引擎状态（C1）：驱动解析面板双轨语义（docling=隐藏滑窗参数）。
 *  后端配置静态（kb_parser_engine），staleTime 放宽为 5min。 */
export function useKbParserEngine(enabled: boolean) {
  const { kb_enabled } = useCapabilities()
  return useQuery({
    queryKey: queryKeys.kbParserEngine,
    queryFn: knowledgeApi.parserEngine,
    enabled: enabled && kb_enabled,
    retry: false,
    staleTime: 300_000,
  })
}

/** 单库文档列表：存在 pending/indexing 文档时每 2s 轮询，全终态后自动停。
 *  retry:false 避免接口异常时反复重试刷屏。 */
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

/** 手动解析：参数持久化 + 后台入库（幂等；重新解析调同一接口换参数即可） */
export function useParseDocument(kbId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ docId, body }: { docId: UUID; body?: KbParseRequest }) =>
      knowledgeApi.parseDocument(kbId, docId, body),
    onSuccess: () => invalidateKb(qc, kbId),
  })
}

/** 检索调试（按需触发，不缓存）。409=维度漂移，detail 直接展示。 */
export function useKbSearch(kbId: UUID) {
  return useMutation({
    mutationFn: ({ q, k }: { q: string; k: number }) => knowledgeApi.search(kbId, q, k),
  })
}
