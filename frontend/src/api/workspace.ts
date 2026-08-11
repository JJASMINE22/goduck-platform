/* 工作区 API —— 对应后端 api/workspace.py（prefix /sessions，session 作用域）。
   user_id 不由前端传（取自 session，路径同源 sanitize）。下载走浏览器原生导航（URL 直链）。 */
import { ApiClient, get } from './client'
import type {
  UUID,
  WorkspaceCapability,
  WorkspaceFileContent,
  WorkspaceNode,
  WorkspacePreview,
} from '@/types'

const wsBase = (sessionId: UUID) => `/sessions/${sessionId}/workspace`

export const workspaceApi = {
  /** 工作区能力查询 —— 驱动面板可见性（准则 F2 能力驱动 UI） */
  capability: (sessionId: UUID) =>
    get<WorkspaceCapability>(`/sessions/${sessionId}/workspace-capability`),

  /** 递归文件树（受限条目/深度）；空工作区返回 [] */
  tree: (sessionId: UUID) => get<WorkspaceNode[]>(`${wsBase(sessionId)}/tree`),

  /** 读文本文件内容（带截断；二进制仅元信息，is_text=false） */
  file: (sessionId: UUID, path: string) =>
    get<WorkspaceFileContent>(`${wsBase(sessionId)}/file`, { path }),

  /** office 文档 → markitdown Markdown（docx/xlsx/pptx/pdf/csv 等） */
  preview: (sessionId: UUID, path: string) =>
    get<WorkspacePreview>(`${wsBase(sessionId)}/preview`, { path }),

  /** 下载直链（单文件 RFC5987 中文文件名；目录为 zip）—— 浏览器原生导航/另存为 */
  downloadUrl: (sessionId: UUID, path: string) =>
    `${ApiClient.BASE_URL}${wsBase(sessionId)}/download?path=${encodeURIComponent(path)}`,
}
