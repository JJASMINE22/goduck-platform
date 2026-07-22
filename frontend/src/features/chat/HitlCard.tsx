/* HITL 决策卡 —— interrupt 事件到达后渲染。对应后端 schemas.Decision。
   approve/edit/reject/respond 四类决策 → resume(decisions)。edit 时编辑 action.args（JSON）。 */
import { Button, Flex, Input, Space, Tag, Typography } from 'antd'
import { type ReactNode, useState } from 'react'
import { Check, Edit3, MessageCircleReply, X } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { Decision, DecisionType, InterruptPayload } from '@/types'
import { toastError } from '@/lib/antdStatic'

export function HitlCard({ resume }: { resume: (decisions: Decision[]) => Promise<void> }): ReactNode {
  const pending = useChatStore((s) => s.pendingInterrupt)
  const [mode, setMode] = useState<DecisionType | null>(null)
  const [message, setMessage] = useState('')
  const [argsText, setArgsText] = useState('')

  if (!pending) return null
  const payload: InterruptPayload = pending.payload
  const firstAction = payload.action_requests[0]?.action
  const name = firstAction?.name ?? '未知工具'
  const args = firstAction?.args ?? {}

  const submit = (type: DecisionType) => {
    let decision: Decision = { type }
    if (type === 'edit') {
      let edited: Record<string, unknown>
      try {
        edited = argsText.trim() ? JSON.parse(argsText) : args
      } catch {
        toastError('args 不是合法 JSON')
        return
      }
      decision = { type: 'edit', edited_action: { name, args: edited } }
    } else if (type === 'reject' || type === 'respond') {
      decision = { type, message: message || null }
    }
    setMode(null)
    setMessage('')
    setArgsText('')
    void resume([decision])
  }

  return (
    <Flex justify="flex-start">
      <div
        style={{
          width: '78%',
          border: '1px solid var(--color-warning)',
          background: 'var(--color-bg-elevated)',
          borderRadius: 12,
          padding: 14,
        }}
      >
        <Flex align="center" gap={8} style={{ marginBottom: 8 }}>
          <Tag color="orange">人工审批</Tag>
          <Typography.Text strong>
            工具调用待审批：<code>{name}</code>
          </Typography.Text>
        </Flex>
        <pre style={{ margin: '0 0 12px', background: 'var(--color-border-soft)', padding: 8, borderRadius: 6, fontSize: 12, overflow: 'auto', maxHeight: 200 }}>
          {JSON.stringify(args, null, 2)}
        </pre>

        {mode === 'edit' ? (
          <div style={{ marginBottom: 12 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              编辑后的参数（JSON）
            </Typography.Text>
            <Input.TextArea
              rows={5}
              value={argsText}
              onChange={(e) => setArgsText(e.target.value)}
              placeholder={JSON.stringify(args, null, 2)}
              style={{ fontFamily: 'var(--font-mono)', fontSize: 12, marginTop: 4 }}
            />
          </div>
        ) : null}

        {(mode === 'reject' || mode === 'respond') && (
          <div style={{ marginBottom: 12 }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {mode === 'reject' ? '拒绝说明（可选）' : '回复内容'}
            </Typography.Text>
            <Input.TextArea
              rows={2}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              style={{ marginTop: 4 }}
            />
          </div>
        )}

        <Space wrap>
          {mode === null ? (
            <>
              <Button type="primary" icon={<Check size={14} />} onClick={() => submit('approve')}>
                通过
              </Button>
              <Button icon={<Edit3 size={14} />} onClick={() => setMode('edit')}>
                编辑后通过
              </Button>
              <Button icon={<MessageCircleReply size={14} />} onClick={() => setMode('respond')}>
                直接回复
              </Button>
              <Button danger icon={<X size={14} />} onClick={() => setMode('reject')}>
                拒绝
              </Button>
            </>
          ) : (
            <>
              <Button type="primary" onClick={() => submit(mode)}>
                确认{mode === 'edit' ? '编辑' : mode === 'reject' ? '拒绝' : '回复'}
              </Button>
              <Button onClick={() => setMode(null)}>取消</Button>
            </>
          )}
        </Space>
      </div>
    </Flex>
  )
}
