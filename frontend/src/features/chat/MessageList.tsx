/* 聊天消息流 —— 合并 chatStore.messages + toolCalls 按 createdAt 排序成统一时间线。
   user/agent 气泡（按 agentPath 区分主/子代理，准则 F2 角色色）+ 工具调用折叠卡。
   [Demo] 入场动画 / 头像 / 角色色带 / 流式打字指示 / 工具执行脉动。 */
import { Collapse, Flex, Tag, Typography } from 'antd'
import { type ReactNode } from 'react'
import { Bot, Check, User, Wrench } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { ChatMessage, ToolCallEntry } from '@/store/chatStore'
import { Markdown } from '@/components/markdown/Markdown'

type TimelineItem =
  | { kind: 'msg'; ts: number; data: ChatMessage }
  | { kind: 'tool'; ts: number; data: ToolCallEntry }

function agentMeta(path: string): { label: string; color: string } {
  if (path === 'root' || path === 'user') return { label: '主代理', color: 'var(--color-role-agent-main)' }
  const seg = path.split('/').pop() ?? path
  return { label: `子代理 · ${seg}`, color: 'var(--color-role-agent-sub)' }
}

function Avatar({ role, color }: { role: 'user' | 'agent'; color: string }): ReactNode {
  return (
    <div className="chat-avatar" style={{ background: role === 'user' ? 'var(--color-primary)' : color }}>
      {role === 'user' ? <User size={17} /> : <Bot size={17} />}
    </div>
  )
}

export function MessageList(): ReactNode {
  const messages = useChatStore((s) => s.messages)
  const toolCalls = useChatStore((s) => s.toolCalls)
  const status = useChatStore((s) => s.status)
  const streaming = status === 'streaming'

  const timeline: TimelineItem[] = [
    ...messages.map((m) => ({ kind: 'msg' as const, ts: m.createdAt, data: m })),
    ...toolCalls.map((t) => ({ kind: 'tool' as const, ts: t.createdAt, data: t })),
  ].sort((a, b) => a.ts - b.ts)

  return (
    <Flex vertical gap={12} style={{ padding: '16px 0', height: '100%' }}>
      {!timeline.length ? (
        <div className="chat-welcome">
          <Bot size={40} />
          <p className="chat-welcome__title">开始对话</p>
          <small className="chat-welcome__hint">在下方输入消息，Enter 发送 / Shift+Enter 换行</small>
        </div>
      ) : null}
      {timeline.map((item) => {
        if (item.kind === 'tool') return <ToolCallCard key={`t-${item.data.id}`} entry={item.data} />
        return <MessageBubble key={item.data.id} msg={item.data} />
      })}
      {streaming ? <TypingIndicator /> : null}
    </Flex>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }): ReactNode {
  const isUser = msg.role === 'user'
  const { label, color } = agentMeta(msg.agentPath)
  return (
    <Flex justify={isUser ? 'flex-end' : 'flex-start'} align="flex-start" gap={10} className="msg-in">
      {!isUser ? <Avatar role="agent" color={color} /> : null}
      <div
        className="chat-bubble"
        style={{
          maxWidth: '78%',
          background: isUser ? 'var(--color-primary-bg)' : 'var(--color-bg-elevated)',
          border: `1px solid ${isUser ? 'var(--color-primary)' : 'var(--color-border)'}`,
          borderLeft: isUser ? undefined : `3px solid ${color}`,
          borderRadius: 12,
          padding: '10px 14px',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        {!isUser ? (
          <div style={{ marginBottom: 4 }}>
            <Tag style={{ color, borderColor: color, background: 'transparent' }}>{label}</Tag>
          </div>
        ) : null}
        {isUser ? (
          <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
        ) : (
          <Markdown>{msg.content}</Markdown>
        )}
      </div>
      {isUser ? <Avatar role="user" color={color} /> : null}
    </Flex>
  )
}

/** 流式生成中：主代理头像 + 三点打字气泡 */
function TypingIndicator(): ReactNode {
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <Avatar role="agent" color="var(--color-role-agent-main)" />
      <div
        className="chat-bubble"
        style={{
          background: 'var(--color-bg-elevated)',
          border: '1px solid var(--color-border)',
          borderLeft: '3px solid var(--color-role-agent-main)',
          borderRadius: 12,
          padding: '12px 14px',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        <span className="typing-dots">
          <span className="typing-dot" />
          <span className="typing-dot" />
          <span className="typing-dot" />
        </span>
      </div>
    </Flex>
  )
}

function ToolCallCard({ entry }: { entry: ToolCallEntry }): ReactNode {
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)', width: 28, height: 28 }}>
        {entry.hasResult ? <Check size={14} /> : <Wrench size={14} className="executing" />}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <Collapse
          size="small"
          items={[
            {
              key: entry.id,
              label: (
                <Flex align="center" gap={6}>
                  <code style={{ fontSize: 12 }}>{entry.name}</code>
                  {entry.caller ? (
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      by {entry.caller}
                    </Typography.Text>
                  ) : null}
                  {entry.hasResult ? (
                    <Tag color="green" style={{ marginInlineStart: 4 }}>已返回</Tag>
                  ) : (
                    <Tag color="processing" style={{ marginInlineStart: 4 }}>执行中</Tag>
                  )}
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
