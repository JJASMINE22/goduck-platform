/* 会话 hooks —— 对应 api/sessions.ts。
   按 user_id 列表；归档失效列表 + 详情。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionsApi } from '@/api/sessions'
import { toastSuccess } from '@/lib/antdStatic'
import type { SessionCreate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useSessions(userId: string | null | undefined, includeArchived = false) {
  return useQuery({
    queryKey: [...queryKeys.sessions(userId ?? ''), { includeArchived }],
    queryFn: () => sessionsApi.listByUser(userId!, includeArchived),
    enabled: !!userId,
  })
}

export function useSession(id: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.session(id ?? ''),
    queryFn: () => sessionsApi.get(id!),
    enabled: !!id,
  })
}

export function useSessionHistory(id: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.sessionHistory(id ?? ''),
    queryFn: () => sessionsApi.history(id!),
    enabled: !!id,
  })
}

export function useCreateSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: SessionCreate) => sessionsApi.create(body),
    onSuccess: (session) =>
      qc.invalidateQueries({ queryKey: queryKeys.sessions(session.user_id) }),
  })
}

export function useArchiveSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => sessionsApi.archive(id),
    onSuccess: (session) => {
      qc.invalidateQueries({ queryKey: queryKeys.sessions(session.user_id) })
      toastSuccess('会话已归档')
    },
  })
}

export function usePurgeSession() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => sessionsApi.purge(id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['sessions'] })
      toastSuccess('会话已彻底删除')
    },
  })
}
