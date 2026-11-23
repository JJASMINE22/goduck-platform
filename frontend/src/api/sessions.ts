/* 会话 API —— 对应后端 api/sessions.py（prefix /sessions）。
   CRUD + checkpointer 历史。流式对话见 stream.ts。 */
import { del, get, post } from './client'
import type {
  FileChangeLogItem,
  KnowledgeCapability,
  MessageOut,
  SessionCreate,
  SessionOut,
  UUID,
  WithdrawRequest,
  WithdrawResponse,
} from '@/types'

export const sessionsApi = {
  create: (body: SessionCreate) => post<SessionOut>('/sessions', body),

  /** 列出当前登录用户的会话（含 agent_name；身份由 cookie 推导） */
  list: (includeArchived = false) =>
    get<SessionOut[]>('/sessions', { include_archived: includeArchived }),

  get: (id: UUID) => get<SessionOut>(`/sessions/${id}`),

  /** 归档会话 */
  archive: (id: UUID) => del<SessionOut>(`/sessions/${id}`),

  /** 彻底删除（物理删 + 清 checkpoint，不可恢复）→ 204 */
  purge: (id: UUID) => post<void>(`/sessions/${id}/purge`),

  /** checkpointer 历史消息（空数组非错误，plan §11.1） */
  history: (id: UUID) => get<MessageOut[]>(`/sessions/${id}/history`),

  /** 工作区文件变更明细（file_change_log 镜像）。anchor=回合锚（末条用户消息
   *  id）时仅取该回合，与 live 侧"本回合聚合卡"语义一致 */
  fileChanges: (id: UUID, anchor?: string) =>
    get<FileChangeLogItem[]>(`/sessions/${id}/file-changes`, {
      ...(anchor ? { anchor } : {}),
    }),

  /** 回撤一个回合（软删）→ 返回原文 + 附件用于回填发送框；流式中后端 409 */
  withdraw: (id: UUID, body: WithdrawRequest) =>
    post<WithdrawResponse>(`/sessions/${id}/withdraw`, body),

  /** 知识库能力查询 —— 驱动会话框 KB 选择器可见性（准则 F2 能力驱动 UI）。
   *  agentId=当前选中智能体（弱绑定解析：显式 > last；缺省回落） */
  knowledgeCapability: (id: UUID, agentId?: UUID | null) =>
    get<KnowledgeCapability>(`/sessions/${id}/knowledge-capability`, {
      ...(agentId ? { agent_id: agentId } : {}),
    }),
}
