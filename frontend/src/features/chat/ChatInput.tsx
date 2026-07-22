/* 聊天输入框 —— TextArea + 发送 + 折叠 rubric 覆盖 + 附件栏。
   发送仅在 status==='idle' 启用；streaming/interrupted 时由消息流/HITL 卡处理。 */
import { Button, Flex, Input, Tooltip, Typography } from 'antd'
import { type ReactNode, useState } from 'react'
import { ChevronDown, ChevronUp, Send, Square } from 'lucide-react'
import { AttachmentBar } from './AttachmentBar'
import type { ChatStreamStatus } from '@/types'
import type { SendOptions } from '@/hooks/useStreamChat'
import type { UUID } from '@/types'

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

  const idle = status === 'idle'
  const streaming = status === 'streaming'

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
    <div style={{ borderTop: '1px solid var(--color-border-soft)', paddingTop: 12, background: 'var(--color-bg-elevated)' }}>
      <AttachmentBar sessionId={sessionId} userId={userId} />

      {showRubric ? (
        <Flex align="center" gap={6} style={{ marginBottom: 6 }}>
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

      <Flex gap={8} align="flex-end">
        <Input.TextArea
          autoSize={{ minRows: 1, maxRows: 6 }}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            idle ? '输入消息，Enter 发送，Shift+Enter 换行' : streaming ? '生成中…（可中止）' : '等待人工审批…'
          }
          style={{ flex: 1 }}
        />
        <Tooltip title={showRubric ? '收起 rubric' : '单次 rubric 覆盖'}>
          <Button icon={showRubric ? <ChevronUp size={14} /> : <ChevronDown size={14} />} onClick={() => setShowRubric((v) => !v)} disabled={!idle} />
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
    </div>
  )
}
