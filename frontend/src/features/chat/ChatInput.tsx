/* 聊天输入 composer（AI Studio 风）—— 单一圆角容器，把附件入口收敛为左下角图标按钮
    （Tooltip + 点击打开文件选择器），已选附件以「缩略图+文件名」chip 嵌在容器内，不再游离于窗口外。
    发送仅在 status==='idle' 启用；streaming/interrupted 时由消息流/HITL 卡处理。
    附件图标独立成入口，便于后续扩展"知识库"筛选（popover 化）。 */
import { Button, Flex, Input, Tooltip, Typography, Upload } from 'antd'
import { type ReactNode, useState } from 'react'
import { ChevronDown, ChevronUp, FileText, Paperclip, Send, Square, X } from 'lucide-react'
import { useAttachmentCapability, useUploadFile } from '@/hooks/useUploads'
import { uploadsApi } from '@/api/uploads'
import { useChatStore } from '@/store/chatStore'
import { toastError } from '@/lib/antdStatic'
import type { ChatStreamStatus, UUID } from '@/types'
import type { SendOptions } from '@/hooks/useStreamChat'

export function ChatInput({
  sessionId,
  userId,
  status,
  send,
  cancel,
}: {
  sessionId: UUID
  userId: string
  status: ChatStreamStatus
  send: (content: string, opts?: SendOptions) => Promise<void>
  cancel: () => void
}): ReactNode {
  const [value, setValue] = useState('')
  const [rubric, setRubric] = useState('')
  const [showRubric, setShowRubric] = useState(false)

  const cap = useAttachmentCapability(sessionId)
  const upload = useUploadFile(sessionId)
  const attachments = useChatStore((s) => s.attachments)
  const addAttachment = useChatStore((s) => s.addAttachment)
  const removeAttachment = useChatStore((s) => s.removeAttachment)

  const idle = status === 'idle'
  const streaming = status === 'streaming'
  const canAttach = !!cap.data?.allow_attachments

  const accept = canAttach
    ? [cap.data!.allow_image ? 'image/*' : null, ...cap.data!.allow_file.map((f) => `.${f}`)]
        .filter(Boolean)
        .join(',')
    : undefined

  const handleFile = async (file: File) => {
    if (!cap.data) return
    if (file.size > cap.data.max_size_mb * 1024 * 1024) {
      toastError(`文件超过 ${cap.data.max_size_mb}MB 限制`)
      return
    }
    try {
      const res = await upload.mutateAsync({ file, userId })
      addAttachment(res)
    } catch {
      /* 错误由全局提示 */
    }
  }

  const handleSend = () => {
    const text = value.trim()
    if (!text || !idle) return
    void send(text, { rubric: rubric.trim() || undefined })
    setValue('')
    setRubric('')
    setShowRubric(false)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat-composer">
      {/* 已选附件（缩略图+文件名 chip，嵌在 composer 内） */}
      {attachments.length ? (
        <Flex gap={8} wrap className="chat-composer__chips">
          {attachments.map((a) => (
            <div key={a.upload_id} className="attach-chip">
              {a.kind === 'image' ? (
                <img
                  className="attach-chip__thumb"
                  src={uploadsApi.previewUrl(sessionId, a.upload_id)}
                  alt={a.filename}
                />
              ) : (
                <span className="attach-chip__icon">
                  <FileText size={16} />
                </span>
              )}
              <span className="attach-chip__name" title={a.filename}>
                {a.filename}
              </span>
              <button
                type="button"
                className="attach-chip__close"
                title="移除"
                onClick={() => removeAttachment(a.upload_id)}
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </Flex>
      ) : null}

      {/* 单次 rubric 覆盖（折叠） */}
      {showRubric ? (
        <Flex align="center" gap={6} className="chat-composer__rubric">
          <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
            本次 rubric：
          </Typography.Text>
          <Input
            size="small"
            value={rubric}
            onChange={(e) => setRubric(e.target.value)}
            placeholder="覆盖该次调用的评分准则（FixedRubric 优先）"
          />
          <Button size="small" type="text" icon={<ChevronUp size={14} />} onClick={() => setShowRubric(false)} />
        </Flex>
      ) : null}

      {/* 输入区（无边框，融入容器） */}
      <Input.TextArea
        className="chat-composer__input"
        variant="borderless"
        autoSize={{ minRows: 1, maxRows: 6 }}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={
          idle ? '输入消息，Enter 发送，Shift+Enter 换行' : streaming ? '生成中…（可中止）' : '等待人工审批…'
        }
      />

      {/* 底部工具条：左=附件图标（AI Studio 风），右=rubric 开关 + 发送/中止 */}
      <Flex justify="space-between" align="center" className="chat-composer__bar">
        <Flex gap={4} align="center">
          {canAttach ? (
            <Upload
              accept={accept || undefined}
              showUploadList={false}
              beforeUpload={(f) => {
                void handleFile(f)
                return false
              }}
            >
              <Tooltip title="上传附件（点击选择文件）" placement="top">
                <Button
                  type="text"
                  shape="circle"
                  size="large"
                  icon={<Paperclip size={18} />}
                  loading={upload.isPending}
                  disabled={!idle}
                />
              </Tooltip>
            </Upload>
          ) : null}
        </Flex>
        <Flex gap={8} align="center">
          <Tooltip title={showRubric ? '收起 rubric' : '单次 rubric 覆盖'}>
            <Button
              type="text"
              shape="circle"
              icon={showRubric ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
              onClick={() => setShowRubric((v) => !v)}
              disabled={!idle}
            />
          </Tooltip>
          {streaming ? (
            <Button danger icon={<Square size={14} />} onClick={cancel}>
              中止
            </Button>
          ) : (
            <Button type="primary" icon={<Send size={14} />} disabled={!idle} onClick={handleSend}>
              发送
            </Button>
          )}
        </Flex>
      </Flex>
    </div>
  )
}
