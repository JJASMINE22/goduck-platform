/* 健康检查 hook —— 顶栏红绿灯，30s 轮询，失败不重试（避免后端宕机时刷屏） */
import { useQuery } from '@tanstack/react-query'
import { healthApi } from '@/api/health'
import { queryKeys } from './queryKeys'

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: healthApi.check,
    refetchInterval: 30_000,
    retry: false,
  })
}
