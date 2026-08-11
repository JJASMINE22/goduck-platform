/* 工作区类型 —— 对应后端 models/schemas.py (WorkspaceCapability/Node/FileContent/Preview)
   + api/workspace.py。路径同源 sanitize（写在哪=读在哪，准则 F6 后端是安全）。 */

/** GET /sessions/{id}/workspace-capability —— 驱动会话页工作区面板可见性（准则 F2） */
export interface WorkspaceCapability {
  allow_workspace: boolean
}

/** 工作区文件树节点（递归）。path 为相对用户工作区根的正斜杠相对路径。 */
export interface WorkspaceNode {
  name: string
  path: string
  is_dir: boolean
  size: number
  modified_at?: string | null
  children?: WorkspaceNode[] | null
}

/** GET /sessions/{id}/workspace/file —— 文本文件内容（二进制仅元信息） */
export interface WorkspaceFileContent {
  path: string
  name: string
  size: number
  is_text: boolean
  content?: string | null
  truncated: boolean
  modified_at?: string | null
}

/** GET /sessions/{id}/workspace/preview —— office 文档经 markitdown 转出的 Markdown */
export interface WorkspacePreview {
  path: string
  markdown: string
}
