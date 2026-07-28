/* 聊天页面（DESIGN §10.3）—— SSE 消息流 + HITL 决策 + 附件（能力驱动）+ 历史灌入。
   状态机由 useStreamChat 驱动（准则 F3）。进入会话 reset + 灌历史；auto-scroll 跟随新消息。 */
import { Alert, App as AntApp, Button, Flex, Space, Tag, Typography } from 'antd'
import { type ReactNode, useEffect, useRef } from 'react'
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

  const bottomRef = useRef<HTMLDivElement>(null)

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
    loadHistory(mapped)
  }, [historyQ.data, loadHistory])

  // auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgCount, toolCount])

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
        </Space>
        <Button danger icon={<Trash2 size={14} />} onClick={handleArchive}>
          归档
        </Button>
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
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto', paddingInline: 4 }}>
        <MessageList />
        <HitlCard resume={stream.resume} />
        <div ref={bottomRef} />
      </div>

      {/* 输入 */}
      <ChatInput
        sessionId={session.id}
        userId={session.user_id}
        status={stream.status}
        send={stream.send}
        cancel={stream.cancel}
      />
    </Flex>
  )
}
