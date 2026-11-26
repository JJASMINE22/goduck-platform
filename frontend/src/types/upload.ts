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
  allow_file: string[] // 后端白名单派生（mime_utils 单一来源），当前 csv/docx/html/md/pdf/txt/xlsx
  max_size_mb: number
}

/** mcp image_understand 工具结果的载荷形状（backend/app/mcps/routers/vision.py 契约） */
export interface ImageUnderstandToolPayload {
  result: 'ok' | 'error'
  description?: string // 视觉模型解析描述（markdown 文本）
  ref?: string // MinIO 内容寻址对象键 runtime/{session}/{sha}.{ext}
  model?: string
  message?: string // result=error 时的失败说明
  image_url?: string // 渲染链接：持久 /api/sessions/{id}/runtime-image/... 或 1h 预签名回落
  image_url_hint?: string
}
