/* 聊天输入 composer（AI Studio 风）—— 单一圆角容器，把附件/知识库入口收敛为左下角图标按钮
    （Tooltip + 点击打开选择器），已选项以 chip 嵌在容器内。
    发送仅在 status==='idle' 启用；streaming/interrupted 时由消息流/HITL 卡处理。
    附件图标与知识库图标各自独立成入口（准则 F2 能力驱动 UI：未开启时图标不显示）。 */
import { Button, Checkbox, Flex, Input, Popover, Tooltip, Typography, Upload } from 'antd'
import { type ReactNode, useState } from 'react'
import { BookOpen, ChevronDown, ChevronUp, FileText, Paperclip, Send, Square, X } from 'lucide-react'
import { useAttachmentCapability, useUploadFile } from '@/hooks/useUploads'
import { useKnowledgeBases, useKnowledgeCapability } from '@/hooks/useKnowledge'
import { uploadsApi } from '@/api/uploads'
import { useChatStore } from '@/store/chatStore'
import { toastError } from '@/lib/antdStatic'
import type { ChatStreamStatus, KnowledgeBaseOut, UUID } from '@/types'
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
  // 发送框文本上提到 store（draft）：回撤需回填原文，故 composer 受控于 store 而非局部 state。
  const draft = useChatStore((s) => s.draft)
  const setDraft = useChatStore((s) => s.setDraft)
  const [rubric, setRubric] = useState('')
  const [showRubric, setShowRubric] = useState(false)
  const [kbOpen, setKbOpen] = useState(false)

  // —— 附件 ——
  const cap = useAttachmentCapability(sessionId)
  const upload = useUploadFile(sessionId)
  const attachments = useChatStore((s) => s.attachments)
  const addAttachment = useChatStore((s) => s.addAttachment)
  const removeAttachment = useChatStore((s) => s.removeAttachment)

  // —— 知识库 ——
  const kbCap = useKnowledgeCapability(sessionId)
  const { data: kbs, isLoading: kbsLoading } = useKnowledgeBases()
  const selectedKbs = useChatStore((s) => s.selectedKbs)
  const addKb = useChatStore((s) => s.addKb)
  const removeKb = useChatStore((s) => s.removeKb)

  const idle = status === 'idle'
  const streaming = status === 'streaming'
  const canAttach = !!cap.data?.allow_attachments
  const canUseKb = !!kbCap.data?.allow_knowledge

  const toggleKb = (kb: KnowledgeBaseOut) => {
    if (selectedKbs.some((k) => k.id === kb.id)) {
      removeKb(kb.id)
    } else {
      addKb(kb)
    }
  }

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
    const text = draft.trim()
    if (!text || !idle) return
    void send(text, { rubric: rubric.trim() || undefined })
    setDraft('')
    setRubric('')
    setShowRubric(false)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const kbPopoverContent = (
    <div style={{ minWidth: 240, maxWidth: 300 }}>
      {kbsLoading ? (
        <Typography.Text type="secondary">加载中…</Typography.Text>
      ) : !kbs?.length ? (
        <Typography.Text type="secondary">暂无知识库，请先在知识库页面创建</Typography.Text>
      ) : (
        <Flex vertical gap={2}>
          {kbs.map((kb) => {
            const checked = selectedKbs.some((k) => k.id === kb.id)
            return (
              <div key={kb.id} className="kb-option" onClick={() => toggleKb(kb)}>
                <Checkbox checked={checked} style={{ pointerEvents: 'none' }} />
                <span className="kb-option__name" title={kb.description ?? kb.name}>
                  {kb.name}
                </span>
                {kb.chunk_count ? (
                  <span className="kb-option__meta">{kb.chunk_count} 片段</span>
                ) : null}
              </div>
            )
          })}
        </Flex>
      )}
    </div>
  )

  return (
    <div className="chat-composer">
      {/* 已选附件 + 知识库（chip 嵌在 composer 内） */}
      {attachments.length || selectedKbs.length ? (
        <Flex gap={8} wrap className="chat-composer__chips">
          {selectedKbs.map((kb) => (
            <div key={kb.id} className="kb-chip">
              <BookOpen size={14} />
              <span className="kb-chip__name" title={kb.name}>
                {kb.name}
              </span>
              <button
                type="button"
                className="kb-chip__close"
                title="移除"
                onClick={() => removeKb(kb.id)}
              >
                <X size={13} />
              </button>
            </div>
          ))}
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
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={
          idle ? '输入消息，Enter 发送，Shift+Enter 换行' : streaming ? '生成中…（可中止）' : '等待人工审批…'
        }
      />

      {/* 底部工具条：左=附件/知识库图标，右=rubric 开关 + 发送/中止 */}
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
                  size="middle"
                  icon={<Paperclip size={16} />}
                  loading={upload.isPending}
                  disabled={!idle}
                />
              </Tooltip>
            </Upload>
          ) : null}
          {canUseKb ? (
            <Popover
              trigger="click"
              placement="topLeft"
              open={kbOpen}
              onOpenChange={setKbOpen}
              content={kbPopoverContent}
            >
              <Tooltip title="选择知识库" placement="top">
                <Button
                  type="text"
                  shape="circle"
                  size="middle"
                  icon={<BookOpen size={16} />}
                  disabled={!idle}
                  style={selectedKbs.length ? { color: 'var(--color-primary)' } : undefined}
                />
              </Tooltip>
            </Popover>
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
            <button
              type="button"
              className={`chat-composer__send${idle && draft.trim() ? ' chat-composer__send--ready' : ''}`}
              disabled={!idle}
              onClick={handleSend}
            >
              <Send size={14} />
              发送
            </button>
          )}
        </Flex>
      </Flex>
    </div>
  )
}
