/* 聊天页面（DESIGN §10.3）—— SSE 消息流 + HITL 决策 + 附件（能力驱动）+ 历史灌入。
   状态机由 useStreamChat 驱动（准则 F3）。进入会话 reset + 灌历史；auto-scroll 跟随新消息。 */
import { Alert, App as AntApp, Button, Flex, Space, Tag, Typography } from 'antd'
import { type ReactNode, useCallback, useEffect, useRef } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Trash2 } from 'lucide-react'
import { useSession, useSessionHistory, useArchiveSession } from '@/hooks/useSessions'
import { useStreamChat } from '@/hooks/useStreamChat'
import { useChatStore } from '@/store/chatStore'
import { MessageList } from '@/features/chat/MessageList'
import { HitlCard } from '@/features/chat/HitlCard'
import { ChatInput } from '@/features/chat/ChatInput'
import { PageLoading } from '@/components/feedback/states'

export function ChatPage(): ReactNode {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const sessionId = id ?? ''

  const sessionQ = useSession(sessionId || null)
  const historyQ = useSessionHistory(sessionId || null)
  const archiveSession = useArchiveSession()
  const { modal } = AntApp.useApp()

  const stream = useStreamChat(sessionId || null)
  const reset = useChatStore((s) => s.reset)
  const loadHistory = useChatStore((s) => s.loadHistory)
  const streamError = useChatStore((s) => s.streamError)
  const msgCount = useChatStore((s) => s.messages.length)
  const toolCount = useChatStore((s) => s.toolCalls.length)

  const scrollRef = useRef<HTMLDivElement>(null)
  // 是否"贴底"：用户没主动上滑翻历史。发消息/新条目出现时强制置 true。
  const pinnedRef = useRef(true)

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96
  }, [])

  // 会话切换：重置 store
  useEffect(() => {
    reset()
  }, [sessionId, reset])

  // 历史灌入（首次加载）
  useEffect(() => {
    if (!historyQ.data) return
    const mapped = historyQ.data
      .filter((m) => m.role === 'human' || m.role === 'ai')
      .map((m, i) => ({
        id: m.id ?? `h${i}`,
        role: (m.role === 'human' ? 'user' : 'agent') as 'user' | 'agent',
        agentPath: 'root',
        content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        createdAt: i,
      }))
      // 跳过纯工具调用的空 AI 消息：实时流仅在 msg.content 非空时才产出气泡，
      // 历史灌入需保持一致，避免出现空气泡。
      .filter((m) => m.role === 'user' || m.content.trim() !== '')
    loadHistory(mapped)
  }, [historyQ.data, loadHistory])

  // 新消息 / 工具卡出现：用户发起动作 → 强制贴底滚到最新
  useEffect(() => {
    pinnedRef.current = true
    scrollToBottom()
  }, [msgCount, toolCount, scrollToBottom])

  // token 流式增长：仅在贴底时跟随（用户上滑翻历史则不打扰）。
  // 订阅 store 而非派生状态，避免逐 token 重渲染整页；rAF 等布局就绪再滚动。
  useEffect(() => {
    const unsub = useChatStore.subscribe(() => {
      if (!pinnedRef.current) return
      requestAnimationFrame(scrollToBottom)
    })
    return unsub
  }, [scrollToBottom])

  if (sessionQ.isLoading) return <PageLoading tip="加载会话…" />
  if (!sessionQ.data) {
    return (
      <Alert
        type="warning"
        message="会话不存在"
        action={<Button onClick={() => navigate('/sessions')}>返回列表</Button>}
      />
    )
  }

  const session = sessionQ.data
  const archived = session.status === 'archived'

  const handleArchive = () => {
    modal.confirm({
      title: '归档该会话？',
      content: '归档后无法继续对话。',
      okType: 'danger',
      okText: '归档',
      cancelText: '取消',
      onOk: () => archiveSession.mutateAsync(session.id).then(() => navigate('/sessions')),
    })
  }

  return (
    <Flex vertical gap={0} style={{ height: '100%', padding: '16px 20px' }}>
      {/* 顶部 */}
      <Flex justify="space-between" align="center" style={{ paddingBottom: 8 }}>
        <Space>
          <Button icon={<ArrowLeft size={14} />} onClick={() => navigate('/sessions')}>
            返回
          </Button>
          <Typography.Text strong>{session.title || `会话 ${session.id.slice(0, 8)}`}</Typography.Text>
          {session.agent_name ? <Tag color="purple">{session.agent_name}</Tag> : null}
          {stream.status === 'streaming' ? <Tag color="processing">生成中</Tag> : null}
          {stream.status === 'interrupted' ? <Tag color="orange">待审批</Tag> : null}
          {archived ? <Tag color="default">已归档（只读）</Tag> : null}
        </Space>
        {archived ? null : (
          <Button danger icon={<Trash2 size={14} />} onClick={handleArchive}>
            归档
          </Button>
        )}
      </Flex>

      {/* 错误提示 */}
      {streamError ? (
        <Alert
          type="error"
          message="会话出错"
          description={streamError}
          closable
          style={{ margin: '8px 0' }}
        />
      ) : null}

      {/* 消息流 */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{ flex: 1, minHeight: 0, overflow: 'auto', paddingInline: 4 }}
      >
        <MessageList />
        {!archived ? <HitlCard resume={stream.resume} /> : null}
      </div>

      {/* 输入：归档会话只读，不可继续对话 */}
      {archived ? (
        <Alert
          type="info"
          message="该会话已归档，仅支持查看历史记录，不可继续对话。"
          style={{ marginTop: 8 }}
        />
      ) : (
        <ChatInput
          sessionId={session.id}
          userId={session.user_id}
          status={stream.status}
          send={stream.send}
          cancel={stream.cancel}
        />
      )}
    </Flex>
  )
}
