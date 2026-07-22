/* 健康检查 API —— 对应后端 GET /health（main.py）。顶栏红绿灯用。 */
import { get } from './client'
import type { HealthStatus } from '@/types'

export const healthApi = {
  check: () => get<HealthStatus>('/health'),
}
