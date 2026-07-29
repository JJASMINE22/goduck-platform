/* 附件 API —— 对应后端 api/uploads.py（prefix /sessions）。
   关键（准则 F6）：前端只传 file（含 filename）+ user_id，不传 mime/kind（服务端魔数判定）。 */
import { ApiClient, get, postForm } from './client'
import type { AttachmentCapability, UUID, UploadOut } from '@/types'

export const uploadsApi = {
  /** multipart 上传 → MinIO + DB，返回完整元数据 */
  upload: (sessionId: UUID, file: File, userId: string) => {
    const form = new FormData()
    form.append('file', file, file.name) // 保留 filename（docx 扩展名判定需要）
    form.append('user_id', userId)
    return postForm<UploadOut>(`/sessions/${sessionId}/uploads`, form)
  },

  /** 附件能力查询 —— 驱动上传组件可见性（准则 F2 能力驱动 UI） */
  capability: (sessionId: UUID) =>
    get<AttachmentCapability>(`/sessions/${sessionId}/attachment-capability`),

  /** 图片缩略图直链（后端从 MinIO 透出原始字节；仅 image 有，docx 该地址 404） */
  previewUrl: (sessionId: UUID, uploadId: UUID) =>
    `${ApiClient.BASE_URL}/sessions/${sessionId}/uploads/${uploadId}/preview`,
}
