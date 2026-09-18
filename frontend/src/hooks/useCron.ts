/* 定时任务 hooks —— 对应 api/cron.ts。
   列表按 user_id；变更后失效列表（scheduler 后台推进的 next_run_at/last_status
   由列表的 staleTime 自然刷新，无需订阅推送）。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { cronApi } from '@/api/cron'
import { toastSuccess } from '@/lib/antdStatic'
import type { CronJobCreate, CronJobUpdate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useCronJobs(userId: string | null | undefined, refetchMs = 30_000) {
  return useQuery({
    queryKey: queryKeys.cronJobs(userId ?? ''),
    queryFn: () => cronApi.listByUser(userId!),
    enabled: !!userId,
    // 任务状态（running/awaiting_approval/下次运行时间）由后端 tick 推进，
    // 30s 的页面停留刷新足以跟上默认 30s 的调度节奏；任务会话页以更密间隔
    // 跟随运行状态（徽标/HITL 信号）
    refetchInterval: refetchMs,
  })
}

export function useCronRuns(jobId: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.cronRuns(jobId ?? ''),
    queryFn: () => cronApi.runs(jobId!),
    enabled: !!jobId,
  })
}

export function useCreateCronJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: CronJobCreate) => cronApi.create(body),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: queryKeys.cronJobs(job.user_id) })
      // 专用任务会话同时落库：会话列表随之出现（带 kind='cron' 徽标）
      qc.invalidateQueries({ queryKey: ['sessions'] })
    },
  })
}

export function useUpdateCronJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: CronJobUpdate }) => cronApi.update(id, body),
    onSuccess: (job) => {
      qc.invalidateQueries({ queryKey: queryKeys.cronJobs(job.user_id) })
      qc.invalidateQueries({ queryKey: queryKeys.cronRuns(job.id) })
    },
  })
}

export function useDeleteCronJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => cronApi.remove(id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['cron'] })
      qc.invalidateQueries({ queryKey: ['sessions'] })
      toastSuccess('任务已删除（产出会话已归档保留）')
    },
  })
}

export function useRunCronJobNow() {
  return useMutation({
    mutationFn: (id: UUID) => cronApi.runNow(id),
    onSuccess: () => toastSuccess('已触发运行，稍后在运行历史查看'),
  })
}
