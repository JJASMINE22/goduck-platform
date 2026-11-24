/* Rubric 能力 hook —— 对应 api/sessions.ts rubricCapability。
   - 会话框「本次 rubric 覆盖」入口级查询；按 sessionId 启用，query key 含 agentId
     （切换选中智能体即 keyed refetch）。
   - allow_rubric=false（智能体未启用 rubric，或为常驻 fixed 模式）时入口不渲染，不重试。 */
import { useQuery } from '@tanstack/react-query'
import { sessionsApi } from '@/api/sessions'
import type { UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useRubricCapability(
  sessionId: UUID | null | undefined,
  agentId?: UUID | null,
) {
  return useQuery({
    queryKey: queryKeys.rubricCapability(sessionId ?? '', agentId ?? null),
    queryFn: () => sessionsApi.rubricCapability(sessionId!, agentId ?? null),
    enabled: !!sessionId,
    retry: false,
  })
}
