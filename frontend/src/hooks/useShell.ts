/* Shell 能力 hook —— 对应 api/shell.ts。
   - 会话页徽标级查询；按 sessionId 启用。
   - 全局未启用（enable_shell=false）时后端返回 allow_shell=false，不重试。 */
import { useQuery } from '@tanstack/react-query'
import { shellApi } from '@/api/shell'
import type { UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useShellCapability(
  sessionId: UUID | null | undefined,
  agentId?: UUID | null,
) {
  return useQuery({
    queryKey: queryKeys.shellCapability(sessionId ?? '', agentId ?? null),
    queryFn: () => shellApi.capability(sessionId!, agentId ?? null),
    enabled: !!sessionId,
  })
}
