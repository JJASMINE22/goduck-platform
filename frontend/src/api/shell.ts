/* Shell 能力 API —— 对应后端 api/stream.py（prefix /sessions，session 作用域）。
   user_id 不由前端传（取自 session）。仅一个 capability 查询（execute 结果经 SSE 流回）。 */
import { get } from './client'
import type { ShellCapability, UUID } from '@/types'

export const shellApi = {
  /** Shell 能力查询 —— 驱动会话页徽标可见性（准则 F2 能力驱动 UI） */
  capability: (sessionId: UUID) =>
    get<ShellCapability>(`/sessions/${sessionId}/shell-capability`),
}
