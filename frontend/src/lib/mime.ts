/* MIME / kind 展示工具 —— 仅前端只读展示，类型判定以服务端魔数为唯一可信源（准则 F6）。
   不在此处做任何判定逻辑，只把后端返回的 mime/kind 映射成图标名和展示文案。 */
import { FileText, Image as ImageIcon } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export type AttachmentKind = 'image' | 'file'

/** kind → 图标（附件预览展示用） */
export function kindIcon(kind: AttachmentKind | string | null | undefined): LucideIcon {
  return kind === 'image' ? ImageIcon : FileText
}

/** kind → 中文展示名 */
export function kindLabel(kind: AttachmentKind | string | null | undefined): string {
  return kind === 'image' ? '图片' : '文档'
}

/** mime → 人类可读（image/png → PNG） */
export function mimeLabel(mime: string | null | undefined): string {
  if (!mime) return '未知'
  const slash = mime.lastIndexOf('/')
  const sub = slash >= 0 ? mime.slice(slash + 1) : mime
  // OOXML 长 mime 取通用缩写：wordprocessingml/spreadsheetml → DOCX/XLSX
  if (sub.includes('wordprocessing')) return 'DOCX'
  if (sub.includes('spreadsheetml')) return 'XLSX'
  if (sub === 'plain') return 'TXT'
  if (sub === 'markdown') return 'MD'
  return sub.toUpperCase()
}
