/* 聊天页面（DESIGN §10.3）—— SSE 消息流 + HITL 决策 + 附件（能力驱动）+ 历史灌入。
   状态机由 useStreamChat 驱动（准则 F3）。进入会话 reset + 灌历史；auto-scroll 跟随新消息。
   布局：消息流居中阅读列（修"显示不全"）+ 右侧可拖拽上下文面板 + 输入框可拖拽改高。 */
import { Alert, App as AntApp, Button, Flex, Space, Tag, Typography } from 'antd'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Folder, Trash2 } from 'lucide-react'
import { useSession, useSessionHistory, useArchiveSession } from '@/hooks/useSessions'
import { useStreamChat } from '@/hooks/useStreamChat'
import { useWorkspaceCapability } from '@/hooks/useWorkspace'
import { useChatStore } from '@/store/chatStore'
import { MessageList } from '@/features/chat/MessageList'
import { HitlCard } from '@/features/chat/HitlCard'
import { ChatInput } from '@/features/chat/ChatInput'
import { WorkspacePanel } from '@/features/workspace/WorkspacePanel'
import { PageLoading } from '@/components/feedback/states'
import { sessionsApi } from '@/api/sessions'
import { ApiError } from '@/api/client'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import type { WithdrawResponse } from '@/types'

const clamp = (min: number, max: number, v: number) => Math.min(max, Math.max(min, v))

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

/** 把 store 中的用户消息（可能是 live 合成本地 id）映射到 LangGraph 真实 id。
    依据：store 中用户消息按发送顺序排列，与历史 human 消息序列 1:1 对齐
    （用户消息从不被 reasoning/空消息过滤，序数稳定可靠）。 */
async function resolveHumanId(sessionId: string, storeMsgId: string): Promise<string> {
  const store = useChatStore.getState()
  const ord = store.messages
    .filter((m) => m.role === 'user')
    .findIndex((m) => m.id === storeMsgId)
  if (ord < 0) throw new Error('找不到该回合')
  const history = await sessionsApi.history(sessionId)
  const realId = history.filter((m) => m.role === 'human')[ord]?.id
  if (!realId) throw new Error('无法定位该回合的服务端消息 id')
  return realId
}

/** 调用回撤；后端在流式生成期间返回 409，按退避重试（场景①：停止后后端释放 tracker 的短暂窗口）。 */
async function callWithdraw(
  sessionId: string,
  messageId: string,
  attempts = 4,
): Promise<WithdrawResponse> {
  let lastErr: unknown
  for (let i = 0; i < attempts; i++) {
    try {
      return await sessionsApi.withdraw(sessionId, { message_id: messageId })
    } catch (e) {
      lastErr = e
      // 仅对"流式生成中"的 409 退避重试，其余错误立即抛出
      if (e instanceof ApiError && e.status === 409 && /流式|stream|生成/i.test(e.message)) {
        await sleep(350)
        continue
      }
      throw e
    }
  }
  throw lastErr
}

export function ChatPage(): ReactNode {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const sessionId = id ?? ''

  const sessionQ = useSession(sessionId || null)
  const historyQ = useSessionHistory(sessionId || null)
  const archiveSession = useArchiveSession()
  const { modal } = AntApp.useApp()

  const stream = useStreamChat(sessionId || null)
  const wsCapQ = useWorkspaceCapability(sessionId || null)
  const allowWorkspace = wsCapQ.data?.allow_workspace === true
  const [wsOpen, setWsOpen] = useState(false)
  const reset = useChatStore((s) => s.reset)
  const loadHistory = useChatStore((s) => s.loadHistory)
  const streamError = useChatStore((s) => s.streamError)
  // 回撤所需 store 动作
  const removeTurn = useChatStore((s) => s.removeTurn)
  const setDraft = useChatStore((s) => s.setDraft)
  const setAttachments = useChatStore((s) => s.setAttachments)
  const clearInterrupt = useChatStore((s) => s.clearInterrupt)
  const setStatus = useChatStore((s) => s.setStatus)
  const [withdrawingId, setWithdrawingId] = useState<string | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  // 是否"贴底"：用户没主动上滑翻历史。发消息/新条目出现时强制置 true。
  const pinnedRef = useRef(true)

  // —— 可拖拽布局状态 ——
  // 输入区高度（纵向拖拽，作为 composer 最小高度）。默认 null=贴合内容（短提示不显瘦高），
  // 拖拽后写入地板值。消息流保持居中阅读列。
  const [inputHeight, setInputHeight] = useState<number | null>(null)

  const onInputResizeDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = inputHeight ?? 96
    // 向上拖 → 输入区变高
    const onMove = (ev: MouseEvent) => setInputHeight(clamp(52, 360, startH + (startY - ev.clientY)))
    const onUp = () => {
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.body.style.cursor = 'row-resize'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

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

  // 历史灌入：仅在数据就绪（非 fetching）时灌入。配合 useSessionHistory 的
  // staleTime:0，重开/切会话都会先 refetch 最新；refetch 进行中不灌入，避免用过时
  // 快照（如新会话的空快照）覆盖刚被 reset() 清空的 store——这正是"重开后历史不可见"的根因。
  useEffect(() => {
    if (!historyQ.data || historyQ.isFetching) return
    const mapped = historyQ.data
      .filter((m) => m.role === 'human' || m.role === 'ai')
      .map((m, i) => ({
        id: m.id ?? `h${i}`,
        role: (m.role === 'human' ? 'user' : 'agent') as 'user' | 'agent',
        agentPath: 'root',
        content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        // human 消息的附件缩略（后端从消息 additional_kwargs 查库回填）
        attachments: m.attachments?.length ? m.attachments : undefined,
        createdAt: i,
      }))
      // 跳过纯工具调用的空 AI 消息：实时流仅在 msg.content 非空时才产出气泡，
      // 历史灌入需保持一致，避免出现空气泡。
      .filter((m) => m.role === 'user' || m.content.trim() !== '')
    loadHistory(mapped)
  }, [historyQ.data, historyQ.isFetching, loadHistory])

  // 用户发起新动作（发消息 / resume 进入 streaming）才强制贴底。
  // 仅监听 status 边沿（idle/interrupted → streaming），而非消息/工具卡数量——
  // 后者在子代理/多线程流式中持续增长，若用作依赖会把"用户上滑浏览历史"
  // 强行拉回底部。流式过程中的贴底跟随由下方 store 订阅按 pinnedRef 处理。
  const prevStatusRef = useRef(stream.status)
  useEffect(() => {
    const prev = prevStatusRef.current
    prevStatusRef.current = stream.status
    if (prev !== 'streaming' && stream.status === 'streaming') {
      pinnedRef.current = true
      scrollToBottom()
    }
  }, [stream.status, scrollToBottom])

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

  /** 回撤一个完整回合：① 流式中先中止 → ② 解析真实 id → ③ 调回撤（409 退避重试）
      → ④ 乐观移除 + 回填草稿/附件 + 退出中断态 → ⑤ 历史重拉兜底一致性。 */
  const handleWithdraw = async (messageId: string) => {
    if (withdrawingId) return
    // 场景①：流式中先停止生成（abort SSE + 置 idle），后端 event_gen 的 finally 会释放 tracker
    if (stream.status === 'streaming') stream.cancel()
    setWithdrawingId(messageId)
    try {
      const realId = await resolveHumanId(session.id, messageId)
      const res = await callWithdraw(session.id, realId)
      removeTurn(messageId)
      setDraft(res.raw_content)
      setAttachments(res.attachments ?? [])
      clearInterrupt()
      setStatus('idle')
      toastSuccess('已回撤该回合，可编辑后重发')
      // 历史失效重拉：以服务端真值兜底（准则 R9），覆盖乐观删除的边界情况
      void historyQ.refetch()
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : '回撤失败，请重试')
    } finally {
      setWithdrawingId(null)
    }
  }

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
          <Space>
            {allowWorkspace ? (
              <Button
                icon={<Folder size={14} />}
                type={wsOpen ? 'primary' : 'default'}
                onClick={() => setWsOpen((o) => !o)}
              >
                工作区
              </Button>
            ) : null}
            <Button danger icon={<Trash2 size={14} />} onClick={handleArchive}>
              归档
            </Button>
          </Space>
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

      {/* 主体：消息流（居中阅读列）+ 行拖拽条 + 输入 composer；右侧可折叠工作区面板 */}
      <Flex gap={12} style={{ flex: 1, minHeight: 0 }}>
        <Flex vertical gap={0} style={{ flex: 1, minWidth: 0 }}>
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            /* overflowY/scrollbar-gutter:stable 预留滚动条空间，避免流式时纵向滚动条
               瞬时弹出与横向布局位移（Windows 经典滚动条占宽）；overflowX:hidden 杜绝
               流式半截内容触发的横向滚动条闪烁。 */
            style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden', scrollbarGutter: 'stable' }}
          >
            {/* 阅读列：放宽上限，避免过窄；气泡完整度已由发光边框保证 */}
            <div style={{ maxWidth: 1180, margin: '0 auto', paddingInline: 4 }}>
              <MessageList
                sessionId={session.id}
                onWithdraw={handleWithdraw}
                withdrawingId={withdrawingId}
                readOnly={archived}
              />
              {!archived ? <HitlCard resume={stream.resume} /> : null}
            </div>
          </div>

          {/* 输入：归档会话只读，不可继续对话 */}
          {archived ? (
            <Alert type="info" message="该会话已归档，仅支持查看历史记录，不可继续对话。" style={{ marginTop: 8 }} />
          ) : (
            <>
              <div className="row-resizer" onMouseDown={onInputResizeDown} title="拖拽调整输入区高度" />
                <div style={{ flexShrink: 0, minHeight: inputHeight ?? undefined, display: 'flex', flexDirection: 'column' }}>
                <ChatInput
                  sessionId={session.id}
                  userId={session.user_id}
                  status={stream.status}
                  send={stream.send}
                  cancel={stream.cancel}
                />
              </div>
            </>
          )}
        </Flex>

        {wsOpen && allowWorkspace ? (
          <WorkspacePanel sessionId={session.id} onClose={() => setWsOpen(false)} />
        ) : null}
      </Flex>
    </Flex>
  )
}
