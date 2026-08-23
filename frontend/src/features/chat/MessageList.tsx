/* 聊天消息流 —— 合并 chatStore.messages + toolCalls 按 createdAt 排序成统一时间线。
   user/agent 气泡（按 agentPath 区分主/子代理，准则 F2 角色色）+ 工具调用折叠卡。
   [Demo] 入场动画 / 头像 / 角色色带 / 流式打字指示 / 工具执行脉动。 */
import { Button, Collapse, Flex, Tag } from 'antd'
import { type CSSProperties, type ReactNode, useMemo } from 'react'
import { Bot, Brain, Undo2, User } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { ChatMessage, FileChangeEntry, ToolCallEntry } from '@/store/chatStore'
import { Markdown } from '@/components/markdown/Markdown'
import { MessageAttachments } from './MessageAttachments'
import { FileChangesCard } from './FileChangesCard'
import { ToolCallCard } from './ToolCallCard'
import type { UUID } from '@/types'

type TimelineItem =
  | { kind: 'msg'; ts: number; data: ChatMessage }
  | { kind: 'tool'; ts: number; data: ToolCallEntry }
  | { kind: 'files'; ts: number; data: FileChangeEntry[] }

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

export function MessageList({
  sessionId,
  onWithdraw,
  withdrawingId,
  readOnly,
  onOpenFile,
}: {
  sessionId: UUID
  onWithdraw: (messageId: string) => void
  withdrawingId: string | null
  readOnly: boolean
  /** 文件变更卡片行点击 → 打开工作区并选中该文件（未开启 workspace 能力时不传，行不可点） */
  onOpenFile?: (path: string) => void
}): ReactNode {
  const messages = useChatStore((s) => s.messages)
  const toolCalls = useChatStore((s) => s.toolCalls)
  const fileChanges = useChatStore((s) => s.fileChanges)
  const status = useChatStore((s) => s.status)
  const streaming = status === 'streaming'

  // 最近一条用户消息 id：流式生成中仅它可"停止并回撤"，其余历史回合禁用
  const lastUserMsgId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') return messages[i].id
    }
    return null
  }, [messages])

  const timeline: TimelineItem[] = [
    ...messages.map((m) => ({ kind: 'msg' as const, ts: m.createdAt, data: m })),
    ...toolCalls.map((t) => ({ kind: 'tool' as const, ts: t.createdAt, data: t })),
    // 单张本回合聚合卡：ts 取末条记录到达时间，随流式新增记录自然贴近时间线底部
    ...(fileChanges.length
      ? [{ kind: 'files' as const, ts: fileChanges[fileChanges.length - 1].createdAt, data: fileChanges }]
      : []),
  ].sort((a, b) => a.ts - b.ts)

  return (
    <Flex vertical gap={14} style={{ padding: '0 0 10px', height: '100%' }}>
      {!timeline.length ? (
        <div className="chat-welcome">
          <Bot size={40} />
          <p className="chat-welcome__title">开始对话</p>
          <small className="chat-welcome__hint">在下方输入消息，Enter 发送 / Shift+Enter 换行</small>
        </div>
      ) : null}
      {timeline.map((item) => {
        if (item.kind === 'tool') return <ToolCallCard key={`t-${item.data.id}`} entry={item.data} />
        if (item.kind === 'files')
          return <FileChangesCard key="file-changes-card" records={item.data} onOpenFile={onOpenFile} />
        if (item.data.reasoning) return <ThinkingBlock key={item.data.id} msg={item.data} />
        return (
          <MessageBubble
            key={item.data.id}
            msg={item.data}
            sessionId={sessionId}
            onWithdraw={onWithdraw}
            withdrawingId={withdrawingId}
            readOnly={readOnly}
            streaming={streaming}
            isLastUser={item.data.id === lastUserMsgId}
          />
        )
      })}
      {streaming ? <TypingIndicator /> : null}
    </Flex>
  )
}

function MessageBubble({
  msg,
  sessionId,
  onWithdraw,
  withdrawingId,
  readOnly,
  streaming,
  isLastUser,
}: {
  msg: ChatMessage
  sessionId: UUID
  onWithdraw: (messageId: string) => void
  withdrawingId: string | null
  readOnly: boolean
  streaming: boolean
  isLastUser: boolean
}): ReactNode {
  const isUser = msg.role === 'user'
  const { label, color } = agentMeta(msg.agentPath)
  const hasAttachments = isUser && !!msg.attachments?.length
  const withdrawing = withdrawingId === msg.id
  // 归档只读会话不可回撤；流式中仅最近一条用户消息可"停止并回撤"，其余禁用
  const canWithdraw = !readOnly && isUser && (streaming ? isLastUser : true)
  return (
    <Flex justify={isUser ? 'flex-end' : 'flex-start'} align="flex-start" gap={10} className="msg-in">
      {!isUser ? <Avatar role="agent" color={color} /> : null}
      {/* msg-turn：气泡 + 悬浮才出现的下方操作条（回撤不放进气泡内）。
          用户回合挂 data-turn-anchor：TurnRail 点击跳转锚点（undefined 时 React 省略属性） */}
      <div
        className={`msg-turn ${isUser ? 'msg-turn--user' : 'msg-turn--agent'}`}
        data-turn-anchor={isUser ? msg.id : undefined}
      >
        <div
          className={`chat-bubble ${isUser ? 'chat-bubble--user' : 'chat-bubble--agent'}`}
          style={{ '--bubble-color': isUser ? 'var(--color-primary)' : color } as CSSProperties}
        >
          {!isUser ? (
            <div style={{ marginBottom: 4 }}>
              <Tag style={{ color, borderColor: color, background: 'transparent' }}>{label}</Tag>
            </div>
          ) : null}
          {msg.streaming ? (
            // 流式接收中：纯文本渲染，避免逐 token 重解析 Markdown（性能 + 防半截语法抖动）。
            // 用户消息无 streaming 标志，恒走 Markdown（支持用户输入的标题/列表/代码等格式）。
            <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
          ) : (
            <Markdown>{msg.content}</Markdown>
          )}
          {hasAttachments ? (
            <MessageAttachments sessionId={sessionId} attachments={msg.attachments!} />
          ) : null}
        </div>
        {/* 回撤入口：气泡下方，hover 整个 turn 才浮现；字号小于正文，弱化视觉 */}
        {canWithdraw ? (
          <div className={`msg-turn__action ${withdrawing ? 'msg-turn__action--visible' : ''}`}>
            <Button
              size="small"
              type="text"
              disabled={!!withdrawingId}
              loading={withdrawing}
              icon={!withdrawing ? <Undo2 size={12} /> : undefined}
              onClick={() => onWithdraw(msg.id)}
            >
              {streaming && isLastUser ? '停止并回撤' : '回撤'}
            </Button>
          </div>
        ) : null}
      </div>
      {isUser ? <Avatar role="user" color={color} /> : null}
    </Flex>
  )
}

/** 思考过程（可折叠，默认收起）—— thinking 事件落为独立消息条，紧邻其后是正式回复 */
function ThinkingBlock({ msg }: { msg: ChatMessage }): ReactNode {
  const { label, color } = agentMeta(msg.agentPath)
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <Avatar role="agent" color={color} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <Collapse
          size="small"
          ghost
          items={[
            {
              key: msg.id,
              label: (
                /* 思考流式进行中（msg.streaming）：仅文字做亮银色流光（.shimmer），
                   图标以透明度脉动配合——背景保持静止 */
                <Flex align="center" gap={6}>
                  <Brain size={13} style={{ color }} className={msg.streaming ? 'executing' : undefined} />
                  <span
                    className={msg.streaming ? 'shimmer' : undefined}
                    style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}
                  >
                    {label} · 思考过程
                  </span>
                </Flex>
              ),
              children: <div className="chat-thinking__body">{msg.reasoning}</div>,
            },
          ]}
        />
      </div>
    </Flex>
  )
}

/** 流式生成中：三个波动点（裸点呈现，不再包气泡框，也不显示头像） */
function TypingIndicator(): ReactNode {
  return (
    <Flex justify="flex-start" align="flex-start" className="msg-in">
      <div className="typing-dots">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </div>
    </Flex>
  )
}
