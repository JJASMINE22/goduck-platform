/* 全局能力开关 hook —— 复用 useHealth 的查询缓存（同 queryKey），不发起额外请求。
   - 启动时即从 /health 拿到 capabilities；30s 轮询随 health 自动刷新。
   - 未加载完成（undefined）时各能力位视为 false：避免渲染可能 404 的入口（F2 安全侧）。
   用于：侧边栏知识库菜单、KB 相关路由/查询的门控。 */
import { useQuery } from '@tanstack/react-query'
import { healthApi } from '@/api/health'
import { queryKeys } from './queryKeys'

export interface CapabilitiesState {
  /** 全部能力已就绪（true=health 已成功返回） */
  ready: boolean
  kb_enabled: boolean
  voice_enabled: boolean
  /** Jev shell 守卫可用（编辑器 shell_guard 开关可见性） */
  jev_enabled: boolean
}

export function useCapabilities(): CapabilitiesState {
  const q = useQuery({
    queryKey: queryKeys.health,
    queryFn: healthApi.check,
    refetchInterval: 30_000,
    retry: false,
  })
  const cap = q.data?.capabilities
  return {
    ready: !!cap,
    kb_enabled: !!cap?.kb_enabled,
    voice_enabled: !!cap?.voice_enabled,
    jev_enabled: !!cap?.jev_enabled,
  }
}
