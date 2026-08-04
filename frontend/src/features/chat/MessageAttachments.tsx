/* 会话窗内的用户附件展示（发送后即时回显）。
   - 图片：antd Image 缩略图 + PreviewGroup 点击放大左右切换
   - 文档：docx/pdf 类型色块卡片（文件名 + 页数/段数 + 大小）
   说明：附件元数据来自发送时的 UploadOut 快照，刷新后历史不回显
   （后端 upload 表无 message 关联，附件已被组装进 HumanMessage.content）。 */
import { Flex, Image, Typography } from 'antd'
import { FileText } from 'lucide-react'
import { type ReactNode } from 'react'
import { uploadsApi } from '@/api/uploads'
import { formatBytes } from '@/lib/format'
import type { UUID, UploadOut } from '@/types'

const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const PDF_MIME = 'application/pdf'

/** mime → 类型徽章（色 + 缩写），仅展示用途 */
function fileBadge(mime: string): { label: string; color: string } {
  if (mime === PDF_MIME) return { label: 'PDF', color: '#E53935' }
  if (mime === DOCX_MIME) return { label: 'DOCX', color: '#2B579A' }
  const ext = mime.split('/').pop()?.toUpperCase() ?? 'FILE'
  return { label: ext, color: '#6B7280' }
}

/** 文档元信息：页数/段数 + 大小 */
function docMeta(a: UploadOut): string {
  const size = formatBytes(a.size)
  if (a.page_count) {
    const unit = a.mime === PDF_MIME ? '页' : '段'
    return `${a.page_count} ${unit} · ${size}`
  }
  return size
}

export function MessageAttachments({
  sessionId,
  attachments,
}: {
  sessionId: UUID
  attachments: UploadOut[]
}): ReactNode {
  if (!attachments?.length) return null
  const images = attachments.filter((a) => a.kind === 'image')
  const files = attachments.filter((a) => a.kind !== 'image')

  return (
    <Flex vertical gap={8} className="msg-attachments">
      {images.length ? (
        <Image.PreviewGroup>
          <Flex gap={8} wrap>
            {images.map((a) => (
              <Image
                key={a.upload_id}
                className="msg-attachments__img"
                src={uploadsApi.previewUrl(sessionId, a.upload_id)}
                alt={a.filename}
                width={132}
                height={132}
                style={{ objectFit: 'cover', borderRadius: 8 }}
              />
            ))}
          </Flex>
        </Image.PreviewGroup>
      ) : null}
      {files.length ? (
        <Flex gap={8} wrap>
          {files.map((a) => {
            const badge = fileBadge(a.mime)
            return (
              <div key={a.upload_id} className="msg-file-card">
                <span className="msg-file-card__badge" style={{ background: badge.color }}>
                  {badge.label}
                </span>
                <span className="msg-file-card__body">
                  <Typography.Text className="msg-file-card__name" title={a.filename}>
                    {a.filename}
                  </Typography.Text>
                  <Typography.Text type="secondary" className="msg-file-card__meta">
                    {docMeta(a)}
                  </Typography.Text>
                </span>
                <FileText size={16} className="msg-file-card__icon" />
              </div>
            )
          })}
        </Flex>
      ) : null}
    </Flex>
  )
}
