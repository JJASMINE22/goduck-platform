/* 附件类型 —— 对应后端 models/schemas.py (UploadOut/AttachmentCapability) +
   api/uploads.py。
   关键：kind/mime 服务端魔数判定，前端只读不传（准则 F6）。 */
import type { UUID } from './common'

/** POST /sessions/{id}/uploads 返回（完整元数据，供前端预览展示） */
export interface UploadOut {
  upload_id: UUID
  kind: 'image' | 'file' // 后端据 mime 判定
  filename: string
  mime: string // 服务端魔数判定结果
  size: number
  page_count?: number | null // 文档段数/页数（eager 提取），图片为 null
  preview?: string | null // 文档首段/首页预览，图片为 null
}

/** GET /sessions/{id}/attachment-capability —— 驱动上传组件可见性（准则 F2） */
export interface AttachmentCapability {
  allow_attachments: boolean
  allow_image: boolean // 取决于 model.supports_vision 或 wrapper 已挂载
  allow_file: string[] // 当前 ["docx", "pdf"]
  max_size_mb: number
}
