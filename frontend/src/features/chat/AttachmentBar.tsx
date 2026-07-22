/* 附件栏（DESIGN §10.4/10.5 能力驱动 UI）—— 由 attachment-capability 决定可见性与可收类型。
   准则 F6：前端只传 file + user_id，mime/kind 服务端魔数判定；前端体积预校验仅体验。 */
import { Button, Flex, Tag, Upload } from 'antd'
import { type ReactNode } from 'react'
import { FileText, Image as ImageIcon, Paperclip, X } from 'lucide-react'
import { useAttachmentCapability, useUploadFile } from '@/hooks/useUploads'
import { useChatStore } from '@/store/chatStore'
import { kindLabel } from '@/lib/mime'
import { toastError } from '@/lib/antdStatic'
import type { UUID } from '@/types'

export function AttachmentBar({ sessionId, userId }: { sessionId: UUID; userId: string }): ReactNode {
  const cap = useAttachmentCapability(sessionId)
  const upload = useUploadFile(sessionId)
  const attachments = useChatStore((s) => s.attachments)
  const add = useChatStore((s) => s.addAttachment)
  const remove = useChatStore((s) => s.removeAttachment)

  if (!cap.data?.allow_attachments) return null

  const accept = [
    cap.data.allow_image ? 'image/*' : null,
    ...cap.data.allow_file.map((f) => `.${f}`),
  ]
    .filter(Boolean)
    .join(',')

  const handleFile = async (file: File) => {
    if (file.size > cap.data!.max_size_mb * 1024 * 1024) {
      toastError(`文件超过 ${cap.data!.max_size_mb}MB 限制`)
      return
    }
    try {
      const res = await upload.mutateAsync({ file, userId })
      add(res)
    } catch {
      /* 错误由全局提示 */
    }
  }

  return (
    <Flex gap={6} wrap align="center" style={{ marginBottom: 8 }}>
      <Upload
        accept={accept || undefined}
        showUploadList={false}
        beforeUpload={(f) => {
          void handleFile(f)
          return false
        }}
      >
        <Button size="small" icon={<Paperclip size={14} />} loading={upload.isPending}>
          添加附件
        </Button>
      </Upload>
      {attachments.map((a) => (
        <Tag
          key={a.upload_id}
          icon={a.kind === 'image' ? <ImageIcon size={12} /> : <FileText size={12} />}
          closeIcon={<X size={12} />}
          closable
          onClose={() => remove(a.upload_id)}
        >
          {a.filename} · {kindLabel(a.kind)}
        </Tag>
      ))}
    </Flex>
  )
}
