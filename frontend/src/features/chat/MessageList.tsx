/* 聊天消息流 —— 合并 chatStore.messages + toolCalls 按 createdAt 排序成统一时间线。
   user/agent 气泡（按 agentPath 区分主/子代理，准则 F2 角色色）+ 工具调用折叠卡。 */
import { Collapse, Flex, Tag, Typography } from 'antd'
import { type ReactNode } from 'react'
import { Wrench } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { ChatMessage, ToolCallEntry } from '@/store/chatStore'
import { Markdown } from '@/components/markdown/Markdown'

type TimelineItem =
  | { kind: 'msg'; ts: number; data: ChatMessage }
  | { kind: 'tool'; ts: number; data: ToolCallEntry }

function agentLabel(path: string): { label: string; color: string } {
  if (path === 'root' || path === 'user') return { label: '主代理', color: 'var(--color-role-agent-main)' }
  const seg = path.split('/').pop() ?? path
  return { label: `子代理 · ${seg}`, color: 'var(--color-role-agent-sub)' }
}

export function MessageList(): ReactNode {
  const messages = useChatStore((s) => s.messages)
  const toolCalls = useChatStore((s) => s.toolCalls)

  const timeline: TimelineItem[] = [
    ...messages.map((m) => ({ kind: 'msg' as const, ts: m.createdAt, data: m })),
    ...toolCalls.map((t) => ({ kind: 'tool' as const, ts: t.createdAt, data: t })),
  ].sort((a, b) => a.ts - b.ts)

  return (
    <Flex vertical gap={12} style={{ padding: '16px 0' }}>
      {timeline.map((item) => {
        if (item.kind === 'tool') return <ToolCallCard key={`t-${item.data.id}`} entry={item.data} />
        return <MessageBubble key={item.data.id} msg={item.data} />
      })}
    </Flex>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }): ReactNode {
  const isUser = msg.role === 'user'
  const { label, color } = agentLabel(msg.agentPath)
  return (
    <Flex justify={isUser ? 'flex-end' : 'flex-start'}>
      <div
        style={{
          maxWidth: '78%',
          background: isUser ? 'var(--color-primary-bg)' : 'var(--color-bg-elevated)',
          border: `1px solid ${isUser ? 'var(--color-primary)' : 'var(--color-border)'}`,
          borderRadius: 12,
          padding: '10px 14px',
        }}
      >
        {!isUser ? (
          <div style={{ marginBottom: 4 }}>
            <Tag color="purple" style={{ color, borderColor: color, background: 'transparent' }}>
              {label}
            </Tag>
          </div>
        ) : null}
        {isUser ? (
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
        ) : (
          <Markdown>{msg.content}</Markdown>
        )}
      </div>
    </Flex>
  )
}

function ToolCallCard({ entry }: { entry: ToolCallEntry }): ReactNode {
  return (
    <Flex justify="flex-start">
      <div style={{ maxWidth: '78%', width: '78%' }}>
        <Collapse
          size="small"
          items={[
            {
              key: entry.id,
              label: (
                <Flex align="center" gap={6}>
                  <Wrench size={13} style={{ color: 'var(--color-role-tool)' }} />
                  <code style={{ fontSize: 12 }}>{entry.name}</code>
                  {entry.caller ? (
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      by {entry.caller}
                    </Typography.Text>
                  ) : null}
                  {entry.hasResult ? <Tag color="green" style={{ marginInlineStart: 4 }}>已返回</Tag> : <Tag>执行中</Tag>}
                </Flex>
              ),
              children: (
                <Flex vertical gap={8}>
                  <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>参数</Typography.Text>
                    <pre style={{ margin: '4px 0', background: 'var(--color-border-soft)', padding: 8, borderRadius: 6, fontSize: 12, overflow: 'auto' }}>
                      {JSON.stringify(entry.args, null, 2)}
                    </pre>
                  </div>
                  {entry.hasResult ? (
                    <div>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>结果</Typography.Text>
                      <pre style={{ margin: '4px 0', background: 'var(--color-border-soft)', padding: 8, borderRadius: 6, fontSize: 12, overflow: 'auto', maxHeight: 240 }}>
                        {entry.result}
                      </pre>
                    </div>
                  ) : null}
                </Flex>
              ),
            },
          ]}
        />
      </div>
    </Flex>
  )
}
