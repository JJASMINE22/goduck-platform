/* 工作区 API —— 对应后端 api/workspace.py（prefix /sessions，session 作用域）。
   user_id 不由前端传（取自 session，路径同源 sanitize）。下载/删除走浏览器原生行为。 */
import { ApiClient, del, get } from './client'
import { downloadUrl } from '@/lib/download'
import type {
  UUID,
  WorkspaceCapability,
  WorkspaceDeleted,
  WorkspaceFileContent,
  WorkspaceNode,
  WorkspacePreview,
} from '@/types'

const wsBase = (sessionId: UUID) => `/sessions/${sessionId}/workspace`

export const workspaceApi = {
  /** 工作区能力查询 —— 驱动面板可见性（准则 F2 能力驱动 UI）。
   *  agentId=当前选中智能体（弱绑定解析：显式 > last；解析落空返回 false） */
  capability: (sessionId: UUID, agentId?: UUID | null) =>
    get<WorkspaceCapability>(`/sessions/${sessionId}/workspace-capability`, {
      ...(agentId ? { agent_id: agentId } : {}),
    }),

  /** 递归文件树（受限条目/深度）；空工作区返回 [] */
  tree: (sessionId: UUID) => get<WorkspaceNode[]>(`${wsBase(sessionId)}/tree`),

  /** 读文本文件内容（带截断；二进制仅元信息，is_text=false） */
  file: (sessionId: UUID, path: string) =>
    get<WorkspaceFileContent>(`${wsBase(sessionId)}/file`, { path }),

  /** office 文档 → markitdown Markdown（docx/xlsx/pptx/pdf/csv 等） */
  preview: (sessionId: UUID, path: string) =>
    get<WorkspacePreview>(`${wsBase(sessionId)}/preview`, { path }),

  /** 下载直链（单文件 RFC5987 中文文件名；目录为 zip）—— 用于 <img>/<iframe> 的 src */
  downloadUrl: (sessionId: UUID, path: string) =>
    `${ApiClient.BASE_URL}${wsBase(sessionId)}/download?path=${encodeURIComponent(path)}`,

  /** 触发浏览器下载（单文件或目录 zip）—— 临时 <a> 点击，避免空白标签页 */
  download: (sessionId: UUID, path: string) =>
    downloadUrl(workspaceApi.downloadUrl(sessionId, path)),

  /** 删除文件或目录（递归删目录）—— 用户裁剪历史产物，不影响智能体逻辑 */
  delete: (sessionId: UUID, path: string) =>
    del<WorkspaceDeleted>(wsBase(sessionId), { path }),
}
