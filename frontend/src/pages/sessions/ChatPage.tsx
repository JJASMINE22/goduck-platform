/* 聊天页面（DESIGN §10.3）—— SSE 消息流 + HITL 决策 + 附件（能力驱动）+ 历史灌入。
   状态机由 useStreamChat 驱动（准则 F3）。进入会话 reset + 灌历史；auto-scroll 跟随新消息。
   布局：页内双视图（会话 | 工作区）display 切换保活 —— 切到工作区时聊天组件不卸载，
   SSE 流与滚动位置不丢（useStreamChat 卸载即中止流的约束）；消息流居中阅读列 +
   输入框可拖拽改高。 */
import { Alert, App as AntApp, Button, Flex, Segmented, Space, Tag, Typography } from 'antd'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, FolderOpen, MessageSquare, Trash2 } from 'lucide-react'
import { useSession, useSessionHistory, useArchiveSession } from '@/hooks/useSessions'
import { useStreamChat } from '@/hooks/useStreamChat'
import { useWorkspaceCapability } from '@/hooks/useWorkspace'
import { queryKeys } from '@/hooks/queryKeys'
import { useShellCapability } from '@/hooks/useShell'
import { useChatStore } from '@/store/chatStore'
import { usePageHeaderStore } from '@/store/pageHeaderStore'
import { MessageList } from '@/features/chat/MessageList'
import { HitlCard } from '@/features/chat/HitlCard'
import { TodoPanel } from '@/features/chat/TodoPanel'
import { TurnRail } from '@/features/chat/TurnRail'
import { ChatInput } from '@/features/chat/ChatInput'
import { WorkspaceView, type WorkspaceSelection } from '@/features/workspace/WorkspaceView'
import { PageLoading } from '@/components/feedback/states'
import { sessionsApi } from '@/api/sessions'
import { ApiError } from '@/api/client'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import type { WithdrawResponse } from '@/types'

const clamp = (min: number, max: number, v: number) => Math.min(max, Math.max(min, v))

/** 页内视图：会话 | 工作区 */
type ChatView = 'chat' | 'workspace'

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
  const shellCapQ = useShellCapability(sessionId || null)
  const allowShell = shellCapQ.data?.allow_shell === true
  const setHeader = usePageHeaderStore((s) => s.setHeader)
  // —— 页内双视图（会话 | 工作区）——
  // display 切换而非卸载：切到工作区时聊天组件保持挂载，流式生成不中断。
  // 状态自带 sessionId 作用域：切换会话时旧值按 scope 在渲染期派生回初始态，
  // 无需 effect 内 setState 重置（避免级联渲染）。
  // selected 为工作区选中文件（受控上提：时间线"文件变更卡片"点击行可跨视图跳转选中）。
  const [wsState, setWsState] = useState<{
    sessionId: string
    view: ChatView
    visited: boolean
    selected: WorkspaceSelection | null
  }>({ sessionId, view: 'chat', visited: false, selected: null })
  const wsView: ChatView = wsState.sessionId === sessionId ? wsState.view : 'chat'
  // 工作区懒挂载 + 保活：首次切入才挂载，此后保持挂载（树展开态/预览选中态在会话内持续）
  const wsVisited = wsState.sessionId === sessionId ? wsState.visited : false
  const wsSelected = wsState.sessionId === sessionId ? wsState.selected : null
  const queryClient = useQueryClient()
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
  // 指针是否悬停在消息流内：流式期间悬停时暂停"追底"滚动——否则持续增长的
  // 内容会把"停止并回撤"等悬浮操作按钮推离光标，hover 断链、根本点不中。
  const hoverRef = useRef(false)

  // —— 可拖拽布局状态 ——
  // 输入区高度（纵向拖拽，作为 composer 最小高度）。默认 null=贴合内容（短提示不显瘦高），
  // 拖拽后写入地板值。消息流保持居中阅读列。
  const [inputHeight, setInputHeight] = useState<number | null>(null)

  const onInputResizeDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    const el = e.currentTarget as HTMLElement
    const startY = e.clientY
    const startH = inputHeight ?? 96
    const onMove = (ev: MouseEvent) => setInputHeight(clamp(52, 360, startH + (startY - ev.clientY)))
    const onUp = () => {
      el.removeAttribute('data-resizing')
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    el.setAttribute('data-resizing', '')
    document.body.style.userSelect = 'none'
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

  /** 时间线"文件变更卡片"点击行 → 一步切到工作区 + 选中该文件 + 树失效刷新。 */
  const openWorkspaceFile = useCallback(
    (path: string) => {
      const name = path.split('/').pop() ?? path
      setWsState({ sessionId, view: 'workspace', visited: true, selected: { path, name } })
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaceTree(sessionId) })
    },
    [sessionId, queryClient],
  )

  /** 工作区选中态受控回调（scope 内更新，跨会话丢弃旧值） */
  const setWsSelected = useCallback(
    (sel: WorkspaceSelection | null) => {
      setWsState((cur) => (cur.sessionId === sessionId ? { ...cur, selected: sel } : cur))
    },
    [sessionId],
  )

  // —— 历史灌入与同会话保留 ——
  // 挂载时判定一次：store 是否已绑定本会话（同会话往返切页）。若是则整个挂载期
  // 跳过 reset + 历史灌入——store 含 SSE 实时增量与工具卡/todos/文件变更等仅存于
  // 前端的富视图状态，/history 只有纯文本快照，任何重灌都是不可逆降级
  // （这正是"切页返回后部分上下文与回合标签消失"的根因）。
  const retainedRef = useRef(false)
  // 显式失效灌入（withdraw 后的服务端真值兜底）：true 时放行一次 loadHistory
  const forcedLoadRef = useRef(false)
  // 失效发起时刻：只接受此后完成的新快照，防 refetch 失败时把撤回前的旧缓存灌回
  const forcedAtRef = useRef(0)

  // 会话切换（或首次进入未绑定的会话）：重置 store；同会话往返：保留 live 状态
  useEffect(() => {
    retainedRef.current = useChatStore.getState().sessionId === sessionId
    forcedLoadRef.current = false
    forcedAtRef.current = 0
    if (!retainedRef.current) reset()
  }, [sessionId, reset])

  // 历史灌入：仅在数据就绪（非 fetching）时灌入。配合 useSessionHistory 的
  // staleTime:0，跨会话进入都会先 refetch 最新；refetch 进行中不灌入，避免用过时
  // 快照覆盖刚被 reset() 清空的 store。
  // 关键防护：历史"从非空突变为空"几乎只可能是后端异常快照（中断流收尾瞬间
  // _load_messages 的 delta 重建偶发返回 []），拒绝用它抹白 store——这正是"中止后重进、
  // 历史全空"的直接诱因。以 store 现存消息数为准（跨会话首灌时 store 已被 reset 清空，
  // 空快照照常灌入）。React Query 出错时保留上次 data，故此处仍能灌入兜底历史。
  useEffect(() => {
    // 同会话往返且未被显式失效：store 即最新真值，跳过一切灌入
    if (retainedRef.current && !forcedLoadRef.current) return
    // 显式失效：只接受失效之后完成的新快照（refetch 失败时 dataUpdatedAt 不变）
    if (forcedLoadRef.current && historyQ.dataUpdatedAt <= forcedAtRef.current) return
    if (!historyQ.data || historyQ.isFetching) return
    if (historyQ.data.length === 0 && useChatStore.getState().messages.length > 0) return
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
    loadHistory(sessionId, mapped)
    forcedLoadRef.current = false
  }, [sessionId, historyQ.data, historyQ.dataUpdatedAt, historyQ.isFetching, loadHistory])

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
    // 流式结束（idle/interrupted/error）：工作区已挂载则刷新文件树，agent 落盘产物即时可见
    if (prev === 'streaming' && stream.status !== 'streaming' && wsVisited) {
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaceTree(sessionId) })
    }
  }, [stream.status, scrollToBottom, wsVisited, queryClient, sessionId])

  // token 流式增长：仅在贴底且指针不在消息流内时跟随（用户上滑翻历史或悬停
  // 瞄准操作按钮时不打扰；离开消息流后若仍贴底则立即追上流式进度）。
  // 订阅 store 而非派生状态，避免逐 token 重渲染整页；rAF 等布局就绪再滚动。
  useEffect(() => {
    const unsub = useChatStore.subscribe(() => {
      if (!pinnedRef.current || hoverRef.current) return
      requestAnimationFrame(scrollToBottom)
    })
    return unsub
  }, [scrollToBottom])

  // 页面标题/动作上提顶部 Header 卡（与列表页一致的模块化布局）。
  // 每次渲染重注册：状态 Tag（生成中/待审批）与动作回调闭包保持新鲜；
  // 早退分支（加载中）也会注册，标题用短 id 兜底。
  useEffect(() => {
    const session = sessionQ.data
    const archived = session?.status === 'archived'
    const effView: ChatView = allowWorkspace && !archived ? wsView : 'chat'
    setHeader({
      title: (
        <Space>
          <Button icon={<ArrowLeft size={14} />} onClick={() => navigate('/sessions')}>
            返回
          </Button>
          <Typography.Text strong>{session?.title || `会话 ${sessionId.slice(0, 8)}`}</Typography.Text>
          {session?.agent_name ? <Tag color="purple">{session.agent_name}</Tag> : null}
          {allowShell ? <Tag color="magenta">Shell</Tag> : null}
          {stream.status === 'streaming' ? <Tag color="processing">生成中</Tag> : null}
          {stream.status === 'interrupted' ? <Tag color="orange">待审批</Tag> : null}
          {archived ? <Tag color="default">已归档（只读）</Tag> : null}
        </Space>
      ),
      extra: archived ? null : (
        <Space>
          {allowWorkspace ? (
            <Segmented
              value={effView}
              onChange={(v) => {
                const next = v as ChatView
                // 切到工作区标记已挂载（此后保活）；scope 内继承 visited/selected，跨会话丢弃旧值
                setWsState((cur) => ({
                  sessionId,
                  view: next,
                  visited: next === 'workspace' || (cur.sessionId === sessionId && cur.visited),
                  selected: cur.sessionId === sessionId ? cur.selected : null,
                }))
              }}
              options={[
                { label: '会话', value: 'chat', icon: <MessageSquare size={13} /> },
                { label: '工作区', value: 'workspace', icon: <FolderOpen size={13} /> },
              ]}
            />
          ) : null}
          <Button
            danger
            icon={<Trash2 size={14} />}
            onClick={() => {
              if (!session) return
              modal.confirm({
                title: '归档该会话？',
                content: '归档后无法继续对话。',
                okType: 'danger',
                okText: '归档',
                cancelText: '取消',
                onOk: () => archiveSession.mutateAsync(session.id).then(() => navigate('/sessions')),
              })
            }}
          >
            归档
          </Button>
        </Space>
      ),
    })
    return () => setHeader(null)
  })

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
  // 渲染期兜底：能力被关闭 / 会话归档时强制回会话视图，避免停留在无切换入口的死角
  const effectiveView: ChatView = allowWorkspace && !archived ? wsView : 'chat'

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
      // 历史失效重拉：以服务端真值兜底（准则 R9），覆盖乐观删除的边界情况。
      // 同会话保留模式下灌入被跳过，必须显式标记"放行一次"，且只接受本次
      // 请求之后完成的新快照（refetch 失败时不得把撤回前的旧缓存灌回）。
      forcedAtRef.current = Date.now()
      forcedLoadRef.current = true
      void historyQ.refetch()
    } catch (e) {
      toastError(e instanceof ApiError ? e.message : '回撤失败，请重试')
    } finally {
      setWithdrawingId(null)
    }
  }

  return (
    <Flex vertical gap={0} style={{ height: '100%' }}>
      {/* 顶部（标题/状态/切换/归档已上提 Header 卡，见上方 setHeader effect） */}

      {/* 错误提示（仅出错时出现，不占常规布局） */}
      {streamError ? (
        <Alert
          type="error"
          message="会话出错"
          description={streamError}
          closable
          style={{ margin: '0 0 8px' }}
        />
      ) : null}

      {/* 主体：页内双视图（会话 | 工作区）display 切换保活 —— 切到工作区时聊天面板
          不卸载，SSE 流与滚动位置不中断/不丢失。会话区包装为独立模块卡片（chat-panel），
          消息流与输入框之间的分隔条内化为卡片内部，默认隐形。 */}
      <Flex gap={12} style={{ flex: 1, minHeight: 0 }}>
        <section className="chat-panel" style={{ display: effectiveView === 'chat' ? 'flex' : 'none' }}>
          {/* body：滚动区 + 回合导航轨的定位上下文（轨不随内容滚、也不伸进 composer） */}
          <div className="chat-panel__body">
            <div
              className="chat-panel__scroll"
              ref={scrollRef}
              onScroll={handleScroll}
              onMouseEnter={() => {
                hoverRef.current = true
              }}
              onMouseLeave={() => {
                hoverRef.current = false
                if (pinnedRef.current) scrollToBottom()
              }}
            >
              {/* 阅读列：放宽上限，避免过窄；气泡完整度已由发光边框保证 */}
              <div className="chat-panel__column">
                {/* 任务规划清单：仅任一代理有 todos 时出现（task_planning 开关驱动） */}
                <TodoPanel />
                <MessageList
                  sessionId={session.id}
                  onWithdraw={handleWithdraw}
                  withdrawingId={withdrawingId}
                  readOnly={archived}
                  onOpenFile={allowWorkspace ? openWorkspaceFile : undefined}
                />
                {!archived ? <HitlCard resume={stream.resume} /> : null}
              </div>
            </div>
            {/* 回合导航轨：≥3 回合才渲染；形态随面板宽度分级（容器查询，见 index.css） */}
            <TurnRail scrollRef={scrollRef} />
          </div>

          {/* 输入：归档会话只读，不可继续对话 */}
          {archived ? (
            <div className="chat-panel__composer">
              <Alert type="info" message="该会话已归档，仅支持查看历史记录，不可继续对话。" />
            </div>
          ) : (
            <>
              <div className="row-resizer" onMouseDown={onInputResizeDown} title="拖拽调整输入区高度" />
              <div className="chat-panel__composer" style={{ minHeight: inputHeight ?? undefined }}>
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
        </section>

        {wsVisited && allowWorkspace && !archived ? (
          <div className={effectiveView === 'workspace' ? 'ws-page' : 'ws-page ws-page--hidden'}>
            <WorkspaceView sessionId={session.id} selected={wsSelected} onSelect={setWsSelected} />
          </div>
        ) : null}
      </Flex>
    </Flex>
  )
}
