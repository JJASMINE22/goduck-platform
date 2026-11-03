/* 定时任务 API —— 对应后端 api/cron.py（prefix /cron）。
   CRUD + 立即触发 + 运行历史；时间字段均为 ISO 8601（后端 timestamptz）。 */
import { del, get, post, put } from './client'
import type { CronJobCreate, CronJobOut, CronJobUpdate, CronRunNowResponse, CronRunOut, UUID } from '@/types'

export const cronApi = {
  create: (body: CronJobCreate) => post<CronJobOut>('/cron/jobs', body),

  /** 列出当前登录用户的任务（含 agent_name / next_run_at / last_status） */
  list: () => get<CronJobOut[]>('/cron/jobs'),

  get: (id: UUID) => get<CronJobOut>(`/cron/jobs/${id}`),

  /** 部分更新：改名/改 prompt/改调度/启停（调度重锚规则见 CronJobUpdate） */
  update: (id: UUID, body: CronJobUpdate) => put<CronJobOut>(`/cron/jobs/${id}`, body),

  /** 删除任务（级联删运行历史；专用会话软归档保留产出）→ 204 */
  remove: (id: UUID) => del<void>(`/cron/jobs/${id}`),

  /** 立即触发一次（不推进 next_run_at；禁用态也可触发，用于补跑/验证）→ 202 */
  runNow: (id: UUID) => post<CronRunNowResponse>(`/cron/jobs/${id}/run`),

  /** 运行历史（最近 50 条） */
  runs: (id: UUID) => get<CronRunOut[]>(`/cron/jobs/${id}/runs`),
}
