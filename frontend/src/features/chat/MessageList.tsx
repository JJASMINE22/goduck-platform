/* 聊天消息流 —— 合并 chatStore.messages + toolCalls 按 createdAt 排序成统一时间线。
   user/agent 气泡（按 agentPath 区分主/子代理，准则 F2 角色色）+ 工具调用折叠卡。
   [Demo] 入场动画 / 头像 / 角色色带 / 流式打字指示 / 工具执行脉动。

   性能（渲染隔离）：流式期间 store 每个合并批（≈30fps）都会更新 messages 引用，
   本列表随之重渲染——但所有子项（气泡/思考块/工具卡/提示行）均为 memo 组件，
   props 除正在增长的那条消息外全部引用稳定（ChatPage 侧已保证回调稳定），
   历史消息在 token 流期间完全跳过重渲染（否则每批成本 = O(会话总内容)，
   长会话吐字/滚动卡顿的主因）。timeline 构建同样入 useMemo。 */
import { Button, Collapse, Flex, Tag } from 'antd'
import { type CSSProperties, type ReactNode, memo, useMemo } from 'react'
import { BookOpen, Brain, OctagonX, Undo2 } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { ChatMessage, FileChangeEntry, ToolCallEntry } from '@/store/chatStore'
import { Markdown } from '@/components/markdown/Markdown'
import { MessageAttachments } from './MessageAttachments'
import { FileChangesCard } from './FileChangesCard'
import { ToolCallCard } from './ToolCallCard'
import { KbSearchToolCard } from './KbSearchToolCard'
import { isKbSearchTool } from './kbSearchTool'
import type { KbRef, UUID } from '@/types'

type TimelineItem =
  | { kind: 'msg'; ts: number; data: ChatMessage }
  | { kind: 'tool'; ts: number; data: ToolCallEntry }
  | { kind: 'files'; ts: number; data: FileChangeEntry[] }

function agentMeta(path: string): { label: string; color: string } {
  if (path === 'root' || path === 'user') return { label: '主代理', color: 'var(--color-role-agent-main)' }
  const seg = path.split('/').pop() ?? path
  return { label: `子代理 · ${seg}`, color: 'var(--color-role-agent-sub)' }
}

/** 空会话标语池（"Duck go, you xxx" 句式，诙谐动词收尾）：
    按会话 id 稳定取模轮换——同会话刷新不变，不同新会话各有惊喜 */
const WELCOME_SLOGANS = [
  'you command, duck go.',
  'you order, duck go.',
  'you demand, duck go.',
  'you insist, duck go.',
  'you instruct, duck go.',
]

/** 会话 id → 稳定标语下标（djb2 风格散列，同 id 恒同值） */
function pickSlogan(sessionId: string): string {
  let h = 5381
  for (let i = 0; i < sessionId.length; i++) h = ((h * 33) ^ sessionId.charCodeAt(i)) | 0
  return WELCOME_SLOGANS[Math.abs(h) % WELCOME_SLOGANS.length]
}

function Avatar({ role }: { role: 'user' | 'agent' }): ReactNode {
  return (
    <div className="chat-avatar chat-avatar--logo">
      <img src={role === 'user' ? '/user_logo.png' : '/assistant_logo.png'} alt={role === 'user' ? '用户' : '智能体'} />
    </div>
  )
}

/** 消息流列表（memo：props 全部由 ChatPage 稳定供给，父组件因输入框拖拽等
    重渲染时整棵消息树跳过；store 订阅在组件内部，流式增量不受影响） */
export const MessageList = memo(function MessageList({
  sessionId,
  onWithdraw,
  withdrawingId,
  readOnly,
  onOpenFile,
}: {
  sessionId: UUID
  /** 回撤回调；任务会话（kind=cron）不提供（后端 409，readOnly 守卫下入口不渲染） */
  onWithdraw?: (messageId: string) => void
  withdrawingId: string | null
  readOnly: boolean
  /** 文件变更卡片行点击 → 打开工作区并选中该文件（未开启 workspace 能力时不传，行不可点） */
  onOpenFile?: (path: string) => void
}): ReactNode {
  const messages = useChatStore((s) => s.messages)
  const toolCalls = useChatStore((s) => s.toolCalls)
  const fileChanges = useChatStore((s) => s.fileChanges)
  const status = useChatStore((s) => s.status)
  const parseStatus = useChatStore((s) => s.parseStatus)
  const streaming = status === 'streaming'
  // 空会话标语（按会话稳定）：品牌徽章 + 一句 quip，不提示使用方式
  const slogan = useMemo(() => pickSlogan(sessionId), [sessionId])

  // 最近一条用户消息 id：流式生成中仅它可"停止并回撤"，其余历史回合禁用
  const lastUserMsgId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') return messages[i].id
    }
    return null
  }, [messages])

  // 统一时间线：三源按 createdAt 归并排序（useMemo——每个 token 合并批都会
  // 触发本组件重渲染，避免重复建数组 + 排序）
  const timeline: TimelineItem[] = useMemo(
    () =>
      [
        ...messages.map((m) => ({ kind: 'msg' as const, ts: m.createdAt, data: m })),
        ...toolCalls.map((t) => ({ kind: 'tool' as const, ts: t.createdAt, data: t })),
        // 单张本回合聚合卡：ts 取末条记录到达时间，随流式新增记录自然贴近时间线底部
        ...(fileChanges.length
          ? [{ kind: 'files' as const, ts: fileChanges[fileChanges.length - 1].createdAt, data: fileChanges }]
          : []),
      ].sort((a, b) => a.ts - b.ts),
    [messages, toolCalls, fileChanges],
  )

  return (
    <Flex vertical gap={14} style={{ padding: '0 0 10px', height: '100%' }}>
      {!timeline.length ? (
        <div className="chat-welcome">
          <div className="chat-welcome__emblem">
            <img src="/logo-transparent.png" alt="goduck" />
          </div>
          <p className="chat-welcome__title">{slogan}</p>
        </div>
      ) : null}
      {timeline.map((item) => {
        // kb_search 专属卡：结构化摘要 + 证据锚定入口（替代平铺 JSON 观测）；
        // 通用工具卡透传 onOpenFile：diff 结果的文件总览行可跳工作区
        if (item.kind === 'tool')
          return isKbSearchTool(item.data.name) ? (
            <KbSearchToolCard key={`t-${item.data.id}`} entry={item.data} />
          ) : (
            <ToolCallCard key={`t-${item.data.id}`} entry={item.data} onOpenFile={onOpenFile} />
          )
        if (item.kind === 'files')
          return <FileChangesCard key="file-changes-card" records={item.data} onOpenFile={onOpenFile} />
        // 手动中断提示条目：渲染为回合末尾的提示行（气泡/工具卡下方），不走 Markdown
        if (item.data.interrupted) return <InterruptedNotice key={item.data.id} />
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
      {streaming ? <TypingIndicator label={parseStatus ?? undefined} /> : null}
    </Flex>
  )
})

/** 消息气泡（memo：msg 引用仅在该条消息更新时变化；sessionId/onWithdraw 等
    由 ChatPage 稳定供给——流式批渲染时历史气泡整体跳过） */
const MessageBubble = memo(function MessageBubble({
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
  onWithdraw?: (messageId: string) => void
  withdrawingId: string | null
  readOnly: boolean
  streaming: boolean
  isLastUser: boolean
}): ReactNode {
  const isUser = msg.role === 'user'
  const { label, color } = agentMeta(msg.agentPath)
  const hasAttachments = isUser && !!msg.attachments?.length
  const hasKbs = isUser && !!msg.kbs?.length
  const withdrawing = withdrawingId === msg.id
  // 归档只读会话不可回撤；流式中仅最近一条用户消息可"停止并回撤"，其余禁用
  const canWithdraw = !readOnly && isUser && (streaming ? isLastUser : true)
  return (
    <Flex justify={isUser ? 'flex-end' : 'flex-start'} align="flex-start" gap={10} className="msg-in">
      {!isUser ? <Avatar role="agent" /> : null}
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
          {hasKbs ? <KbBadges kbs={msg.kbs!} /> : null}
          {msg.streaming ? (
            // 流式接收中：纯文本渲染，避免逐 token 重解析 Markdown（性能 + 防半截语法抖动）。
            // 用户消息无 streaming 标志，恒走 Markdown（支持用户输入的标题/列表/代码等格式）。
            <div style={{ whiteSpace: 'pre-wrap' }}>{msg.content}</div>
          ) : (
            // breaks：用户输入的换行原样呈现（GFM 默认合并单换行，非技术用户难懂
            // "行尾两空格"约定）；markdown 轻语法仍生效，配合气泡内驯化样式保美观。
            <Markdown breaks={isUser}>{msg.content}</Markdown>
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
              onClick={() => onWithdraw?.(msg.id)}
            >
              {streaming && isLastUser ? '停止并回撤' : '回撤'}
            </Button>
          </div>
        ) : null}
      </div>
      {isUser ? <Avatar role="user" /> : null}
    </Flex>
  )
})

/** 用户气泡上的知识库徽标（只读 chip，复用 composer 的 kb-chip 样式；无关闭按钮——
    徽标是历史事实，不提供就地反选）。实时发送（本地快照）与历史回放（/history 回填
    的 {id,name}）同形状，渲染路径唯一 */
function KbBadges({ kbs }: { kbs: KbRef[] }): ReactNode {
  return (
    <Flex gap={6} wrap style={{ marginBottom: 6 }}>
      {kbs.map((kb) => (
        <div key={kb.id} className="kb-chip" title={`已启用知识库：${kb.name}`} style={{ padding: '3px 8px' }}>
          <BookOpen size={14} />
          <span className="kb-chip__name">{kb.name}</span>
        </div>
      ))}
    </Flex>
  )
}

/** 思考过程（可折叠，默认收起）—— thinking 事件落为独立消息条，紧邻其后是正式回复。
    （memo：仅正在流式增长的那条思考块会随合并批重渲染，历史思考块跳过） */
const ThinkingBlock = memo(function ThinkingBlock({ msg }: { msg: ChatMessage }): ReactNode {
  const { label, color } = agentMeta(msg.agentPath)
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <Avatar role="agent" />
      {/* 家族色 = 所属代理角色色（归属语义）：思考过程与该代理气泡同色系，
          但以透明底 + 左条 + 次级正文与气泡区分 */}
      <div className="msg-card" style={{ '--card-family': color } as CSSProperties}>
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
})

/** 流式生成中：三个波动点 + 可选进度文案（附件解析徽标，裸点呈现，不包气泡框/头像） */
const TypingIndicator = memo(function TypingIndicator({ label }: { label?: string }): ReactNode {
  return (
    <Flex justify="flex-start" align="flex-start" className="msg-in">
      <div className="typing-dots">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
        {label ? (
          <span style={{ marginLeft: 6, fontSize: 12, color: 'var(--color-text-tertiary)' }}>{label}</span>
        ) : null}
      </div>
    </Flex>
  )
})

/** 手动中断提示行：用户中止流式后落在本回合末尾（agent 输出气泡下方），
    弱化的虚线左条 + 三级色文字，表达"回合就此截断"而非系统错误 */
const InterruptedNotice = memo(function InterruptedNotice(): ReactNode {
  return (
    <div className="msg-in interrupted-notice">
      <OctagonX size={13} />
      <span>用户已手动中断</span>
    </div>
  )
})
