/* 会话排队发送 API —— 对应后端 api/stream.py 队列管理端点（prefix /sessions）。
   GET /queue 纯读不触发排水；continue/requeue 为显式恢复动作（清暂停 + 排水）。 */
import { del, get, post } from './client'
import type { QueueView, UUID } from '@/types'

export const queueApi = {
  /** 队列真值（终态后回查：决定重挂观察流与气泡物化） */
  getQueue: (sessionId: UUID) => get<QueueView>(`/sessions/${sessionId}/queue`),

  /** [继续]：清除暂停（error/withdrawn/restart 均可）并触发排水 */
  continueQueue: (sessionId: UUID) => post<QueueView>(`/sessions/${sessionId}/queue/continue`),

  /** [清空]：删除全部非 sent 行并清除暂停 */
  clearQueue: (sessionId: UUID) =>
    post<{ ok: boolean; removed: number }>(`/sessions/${sessionId}/queue/clear`),

  /** canceled → pending（保留原 created_at 序）；ids 缺省 = 全部 canceled */
  requeue: (sessionId: UUID, ids?: UUID[]) =>
    post<{ ok: boolean; requeued: number }>(
      `/sessions/${sessionId}/queue/requeue`,
      ids?.length ? { ids } : {},
    ),

  /** 删除单条（pending/canceled/failed；sending 409） */
  deleteItem: (sessionId: UUID, itemId: UUID) =>
    del<void>(`/sessions/${sessionId}/queue/${itemId}`),
}
