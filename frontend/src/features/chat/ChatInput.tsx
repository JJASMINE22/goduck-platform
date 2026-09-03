/* 聊天输入 composer（AI Studio 风）—— 单一圆角容器，把附件/知识库/语音入口收敛为左下角图标按钮
    （Tooltip + 点击打开选择器），已选项以 chip 嵌在容器内。
    发送仅在 status==='idle' 启用；streaming/interrupted 时由消息流/HITL 卡处理。
    附件图标、知识库图标与麦克风（长按说话）各自独立成入口（准则 F2 能力驱动 UI：未开启时不显示）。
    附件三入口共用 useAttachFiles 编排：选择器（可多选）/ 输入框 Ctrl+V 粘贴（截图、复制的
    图片或文件）；上传未返回时渲染 pending chip（图片本地 objectURL 即时缩略 + spinner，
    失败留驻可点重试），全部落定前发送门关闭（快照发生在点击瞬间，防止漏挂在途文件）。
    录音中输入框只读展示"草稿 + 流式转写"合并预览（与后端 stt_output 合并稿同构），
    松开后草稿与终稿合并为一条消息发送，与手打输入互斥。 */
import { Button, Checkbox, Flex, Input, Popover, Tooltip, Typography, Upload } from 'antd'
import { type ReactNode, useRef, useState } from 'react'
import {
  AlertCircle,
  AudioLines,
  BookOpen,
  ChevronDown,
  ChevronUp,
  FileText,
  Loader2,
  Paperclip,
  Send,
  Square,
  X,
} from 'lucide-react'
import { useAttachmentCapability, useAttachFiles } from '@/hooks/useUploads'
import { useKnowledgeBases, useKnowledgeCapability } from '@/hooks/useKnowledge'
import { useCapabilities } from '@/hooks/useCapabilities'
import { uploadsApi } from '@/api/uploads'
import { useChatStore, type PendingAttachment } from '@/store/chatStore'
import type { ChatStreamStatus, KnowledgeBaseOut, UUID } from '@/types'
import type { SendOptions } from '@/hooks/useStreamChat'
import type { UseVoiceInput } from '@/hooks/useVoiceInput'

/** 上传中/失败占位 chip：复用 .attach-chip 骨架。图片以本地 objectURL 即时缩略 +
    状态蒙层（spinner/失败图标）；文档走类型色块。失败态整卡可点重试，X 移除。 */
function PendingChip({
  p,
  onRemove,
  onRetry,
}: {
  p: PendingAttachment
  onRemove: (localId: string) => void
  onRetry: (localId: string) => void
}): ReactNode {
  const failed = p.status === 'error'
  const statusIcon = failed ? (
    <AlertCircle size={15} />
  ) : (
    <Loader2 size={15} className="attach-chip__spin" />
  )
  return (
    <div
      className={`attach-chip attach-chip--${p.status}`}
      title={failed ? '上传失败：点击重试，或点 × 移除' : '上传中…'}
      onClick={failed ? () => onRetry(p.localId) : undefined}
    >
      {p.kindHint === 'image' && p.previewUrl ? (
        <span className="attach-chip__thumbbox">
          <img className="attach-chip__thumb" src={p.previewUrl} alt={p.filename} />
          <span className="attach-chip__mask">{statusIcon}</span>
        </span>
      ) : (
        <span className="attach-chip__icon">{statusIcon}</span>
      )}
      <span className="attach-chip__name" title={p.filename}>
        {p.filename}
      </span>
      <button
        type="button"
        className="attach-chip__close"
        title="移除"
        onClick={(e) => {
          e.stopPropagation()
          onRemove(p.localId)
        }}
      >
        <X size={13} />
      </button>
    </div>
  )
}

export function ChatInput({
  sessionId,
  userId,
  status,
  send,
  cancel,
  voice,
}: {
  sessionId: UUID
  userId: string
  status: ChatStreamStatus
  send: (content: string, opts?: SendOptions) => Promise<void>
  cancel: () => void
  voice: UseVoiceInput
}): ReactNode {
  // 发送框文本上提到 store（draft）：回撤需回填原文，故 composer 受控于 store 而非局部 state。
  const draft = useChatStore((s) => s.draft)
  const setDraft = useChatStore((s) => s.setDraft)
  const [rubric, setRubric] = useState('')
  const [showRubric, setShowRubric] = useState(false)
  const [kbOpen, setKbOpen] = useState(false)

  // —— 附件 ——
  const cap = useAttachmentCapability(sessionId)
  const { attach, retry } = useAttachFiles(sessionId, userId)
  const attachments = useChatStore((s) => s.attachments)
  const removeAttachment = useChatStore((s) => s.removeAttachment)
  const pendingAttachments = useChatStore((s) => s.pendingAttachments)
  const removePending = useChatStore((s) => s.removePending)

  // —— 知识库 ——
  const kbCap = useKnowledgeCapability(sessionId)
  const { data: kbs, isLoading: kbsLoading } = useKnowledgeBases()
  const selectedKbs = useChatStore((s) => s.selectedKbs)
  const addKb = useChatStore((s) => s.addKb)
  const removeKb = useChatStore((s) => s.removeKb)

  // —— 语音（能力驱动：后端 voice_enabled 才显示麦克风）——
  const capabilities = useCapabilities()
  const voiceEnabled = capabilities.voice_enabled
  const voiceState = useChatStore((s) => s.voiceState)
  const interimTranscript = useChatStore((s) => s.interimTranscript)
  const recording = voiceState === 'recording'
  const transcribing = voiceState === 'transcribing'
  // 录音/转写期合并预览：草稿在前 + 流式转写（与后端 stt_output 合并稿同构，
  // 松开后整段作为一条消息发送）
  const voicePreview = draft.trim()
    ? `${draft.trim()}\n${interimTranscript}`
    : interimTranscript

  const idle = status === 'idle'
  const streaming = status === 'streaming'
  const taRef = useRef<HTMLTextAreaElement>(null)
  // 玻璃"点击聚焦"：点击 composer 任意空白处（padding/边缘）即聚焦输入框，
  // 触发 :focus-within 聚焦态特效；交互元素（按钮/输入/popover 内容）不劫持。
  const focusComposer = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest('button, input, textarea, a, .ant-popover')) return
    taRef.current?.focus()
  }
  const canAttach = !!cap.data?.allow_attachments
  const canUseKb = !!kbCap.data?.allow_knowledge
  // 麦克风闸门（与 press() 内部守卫同源）：连接就绪 + 会话空闲 + 无进行中的语音回合
  const micDisabled = !voice.ready || voiceState !== 'idle' || status !== 'idle'

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

  // 占位门：任一附件在途/失败未决时禁止发送（快照在点击瞬间，避免漏挂）；📎 spinner 仅看 uploading
  const hasPending = pendingAttachments.length > 0
  const anyUploading = pendingAttachments.some((p) => p.status === 'uploading')

  // 粘贴上传：截图/复制的图片/复制的文件 → 附件；纯文本粘贴零干预。
  // DataTransferItem 让出事件循环即失效，getAsFile 必须同步收集后再异步上传。
  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!canAttach || !idle || recording || transcribing) return
    const files = Array.from(e.clipboardData.items)
      .filter((it) => it.kind === 'file' && it.type)
      .map((it) => it.getAsFile())
      .filter((f): f is File => f !== null)
    if (!files.length) return
    // 纯文件粘贴（无文本）时阻止向输入框插入无意义占位文本；文本+文件共存（Excel 复制）双行为并存
    if (e.clipboardData.getData('text/plain').trim().length === 0) e.preventDefault()
    attach(files)
  }

  const handleSend = () => {
    const text = draft.trim()
    if (!text || !idle) return
    // 附件在途/未决：禁止发送（见 hasPending 注释）
    if (hasPending) return
    // 语音回合进行中（录音/识别）不与手打发送并发
    if (useChatStore.getState().voiceState !== 'idle') return
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
    <div className="chat-composer" onPointerDown={focusComposer}>
      {/* 已选附件（pending 占位 + 已完成）+ 知识库（chip 嵌在 composer 内） */}
      {attachments.length || pendingAttachments.length || selectedKbs.length ? (
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
          {pendingAttachments.map((p) => (
            <PendingChip key={p.localId} p={p} onRemove={removePending} onRetry={retry} />
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

      {/* 输入区（无边框，融入容器）；录音/转写期间只读展示"草稿+流式转写"合并预览 */}
      <Input.TextArea
        ref={taRef}
        className="chat-composer__input"
        variant="borderless"
        autoSize={{ minRows: 1, maxRows: 6 }}
        value={recording || transcribing ? voicePreview : draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        onPaste={onPaste}
        readOnly={recording || transcribing}
        placeholder={
          recording
            ? '正在听…松开麦克风发送'
            : transcribing
              ? '正在整理语音…'
              : idle
                ? '输入消息，Enter 发送，Shift+Enter 换行'
                : streaming
                  ? '生成中…（可中止）'
                  : '等待人工审批…'
        }
      />

      {/* 底部工具条：左=附件/知识库图标，右=rubric 开关 + 发送/中止 */}
      <Flex justify="space-between" align="center" className="chat-composer__bar">
        <Flex gap={4} align="center">
          {canAttach ? (
            <Upload
              accept={accept || undefined}
              multiple
              showUploadList={false}
              beforeUpload={(f) => {
                // multiple 下 antd 逐文件触发；返回 false 阻止自动上传
                attach([f])
                return false
              }}
            >
              <Tooltip title="上传附件（点击选择或 Ctrl+V 粘贴，可多选）" placement="top">
                <Button
                  type="text"
                  shape="circle"
                  size="middle"
                  icon={<Paperclip size={16} />}
                  loading={anyUploading}
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
          {voiceEnabled ? (
            <Tooltip
              title={
                !voice.ready
                  ? '语音连接建立中…'
                  : recording
                    ? '松开发送'
                    : '按住说话（语音转文字后自动发送）'
              }
              placement="top"
            >
              <button
                type="button"
                className={`chat-composer__mic${recording ? ' chat-composer__mic--active' : ''}`}
                disabled={micDisabled && !recording}
                onPointerDown={(e) => {
                  // 仅主键；按下即起录（start 帧零握手直发）
                  if (e.button !== 0) return
                  e.preventDefault()
                  void voice.press(rubric.trim() || undefined)
                }}
                onPointerUp={() => voice.release()}
                onPointerLeave={() => voice.release()}
                onPointerCancel={() => voice.release()}
                onContextMenu={(e) => e.preventDefault()}
              >
                <AudioLines size={16} />
              </button>
            </Tooltip>
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
            <button type="button" className="glass-btn chat-composer__stop" onClick={cancel}>
              <Square size={14} />
              中止
            </button>
          ) : recording || transcribing ? (
            <span className="chat-composer__voice-hint">
              {recording ? '录音中…' : '识别中…'}
            </span>
          ) : (
            <button
              type="button"
              className={`glass-btn chat-composer__send${idle && draft.trim() && !hasPending ? ' chat-composer__send--ready' : ''}`}
              disabled={!idle || hasPending}
              title={hasPending ? '附件上传中，请稍候…' : undefined}
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
