/* 定时任务类型 —— 对应后端 models/schemas.py (CronJobCreate/Update/Out/CronRunOut) +
   api/cron.py。DB 为唯一真相源：创建/更新落库即生效（scheduler 下个 tick 感知）。 */
import type { ISODateTime, UUID } from './common'

/** 任务运行状态（job.last_status / run.status 同构） */
export type CronJobStatus = 'running' | 'ok' | 'error' | 'awaiting_approval'

export interface CronJobCreate {
  user_id: string
  agent_id: UUID // 须为 kind='cron' 且 active 的智能体（后端 422/404）
  name: string // 同用户内唯一
  prompt: string // 每次触发注入的固定 user query
  interval_minutes: number // 业务下限默认 ≥5（后端配置驱动）
  /** 首次运行时间；null=立即（下个 tick）。过去时间后端夹到当前时刻。interval 相位锚 */
  start_at?: ISODateTime | string | null
}

/** 部分更新（仅显式提供的字段生效；kind 语义见字段注释） */
export interface CronJobUpdate {
  name?: string
  prompt?: string
  interval_minutes?: number
  /** 显式提供（含 null=now）会重锚调度；未提供则不动 */
  start_at?: ISODateTime | string | null
  enabled?: boolean
}

export interface CronJobOut {
  id: UUID
  user_id: string
  agent_id: UUID
  agent_name?: string | null
  session_id: UUID // 专用任务会话（kind='cron'，只读 + HITL 审批）
  name: string
  prompt: string
  interval_minutes: number
  start_at: ISODateTime
  enabled: boolean
  next_run_at?: ISODateTime | null
  last_run_at?: ISODateTime | null
  last_status?: CronJobStatus | null
  last_error?: string | null
  consecutive_failures: number
  created_at: ISODateTime
  updated_at: ISODateTime
}

export type CronRunStatus = CronJobStatus | 'skipped'

export interface CronRunOut {
  id: UUID
  job_id: UUID
  status: CronRunStatus
  trigger: 'scheduled' | 'manual'
  error?: string | null
  started_at: ISODateTime
  finished_at?: ISODateTime | null
}

/** POST /cron/jobs/{id}/run 的 202 响应 */
export interface CronRunNowResponse {
  status: string
}
