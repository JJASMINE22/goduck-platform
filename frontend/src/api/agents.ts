/* 智能体 API —— 对应后端 api/agents.py（prefix /agents）。
   校验/创建/更新/列表/详情/拓扑/历史/还原/导出/归档。 */
import { del, fetchText, get, post, put } from './client'
import type {
  AgentCreate,
  AgentDetail,
  AgentHistoryItem,
  AgentOut,
  AgentUpdate,
  RestoreRequest,
  UUID,
  ValidateRequest,
  ValidateResponse,
} from '@/types'

export const agentsApi = {
  /** dry-run：校验 config + 返回 mermaid 预览（不落库），编辑器实时预览用 */
  validate: (body: ValidateRequest) => post<ValidateResponse>('/agents/validate', body),

  /** 创建（name 唯一，409 冲突）→ 建 registry → 返回 mermaid */
  create: (body: AgentCreate) => post<AgentDetail>('/agents', body),

  /** 更新（级联删会话+清 checkpoint）→ invalidate+rebuild registry */
  update: (id: UUID, body: AgentUpdate) => put<AgentDetail>(`/agents/${id}`, body),

  list: (includeArchived = false) => get<AgentOut[]>('/agents', { include_archived: includeArchived }),

  /** 详情 + 当前 active mermaid（悬浮预览） */
  get: (id: UUID) => get<AgentDetail>(`/agents/${id}`),

  /** Mermaid 文本（PlainTextResponse） */
  topologyMmd: (id: UUID) => fetchText(`/agents/${id}/topology.mmd`),

  /** 配置历史（还原选择） */
  history: (id: UUID) => get<AgentHistoryItem[]>(`/agents/${id}/history`),

  /** 用 history_id 还原 */
  restore: (id: UUID, body: RestoreRequest) => post<AgentDetail>(`/agents/${id}/restore`, body),

  /** 导出当前配置 YAML（PlainTextResponse） */
  exportYaml: (id: UUID) => fetchText(`/agents/${id}/export.yaml`),

  /** 归档（status=archived） */
  archive: (id: UUID) => del<AgentOut>(`/agents/${id}`),

  /** 彻底删除（物理删 + 级联 session/checkpoint/history，不可恢复）→ 204 */
  purge: (id: UUID) => post<void>(`/agents/${id}/purge`),
}
