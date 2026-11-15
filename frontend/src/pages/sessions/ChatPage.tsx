/* 聊天页面（DESIGN §10.3）—— SSE 消息流 + HITL 决策 + 附件（能力驱动）+ 历史灌入。
   状态机由 useStreamChat 驱动（准则 F3）。进入会话 reset + 灌历史；auto-scroll 跟随新消息。
   布局：页内双视图（会话 | 工作区）display 切换保活 —— 切到工作区时聊天组件不卸载，
   滚动位置不丢；离开页面时回合由服务端续跑（run 解耦），重进经观察流补帧。
   消息流居中阅读列 + 输入框可拖拽改高。 */
import { Alert, App as AntApp, Button, Flex, Segmented, Space, Tag, Typography } from 'antd'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronDown, FolderOpen, MessageSquare, Trash2 } from 'lucide-react'
import { useSession, useSessionHistory, useArchiveSession } from '@/hooks/useSessions'
import { useAgents } from '@/hooks/useAgents'
import { useCronJobs } from '@/hooks/useCron'
import { useStreamChat, detachLiveConsumers, ensureObserver } from '@/hooks/useStreamChat'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useWorkspaceCapability } from '@/hooks/useWorkspace'
import { queryKeys } from '@/hooks/queryKeys'
import { useShellCapability } from '@/hooks/useShell'
import { useChatStore } from '@/store/chatStore'
import { usePageHeaderStore } from '@/store/pageHeaderStore'
import { MessageList } from '@/features/chat/MessageList'
import { buildHistoryReplay } from '@/features/chat/historyReplay'
import { HitlCard } from '@/features/chat/HitlCard'
import { TodoPanel } from '@/features/chat/TodoPanel'
import { TurnRail } from '@/features/chat/TurnRail'
import { ChatInput } from '@/features/chat/ChatInput'
import { UsageBadge } from '@/features/chat/UsageBadge'
import { WorkspaceView, type WorkspaceSelection } from '@/features/workspace/WorkspaceView'
import { PageLoading } from '@/components/feedback/states'
import { sessionsApi } from '@/api/sessions'
import { streamApi } from '@/api/stream'
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

/** 调用回撤；后端在流式生成期间返回 409，按退避重试（场景①：停止后后端释放 tracker 的短暂窗口）。
 *  agentId=当前选中智能体（§4.4 弱绑定取图：显式 > last——last 悬空时"仅选中、不发送"即可回撤）。 */
async function callWithdraw(
  sessionId: string,
  messageId: string,
  agentId?: string | null,
  attempts = 4,
): Promise<WithdrawResponse> {
  let lastErr: unknown
  for (let i = 0; i < attempts; i++) {
    try {
      return await sessionsApi.withdraw(sessionId, {
        message_id: messageId,
        agent_id: agentId ?? undefined,
      })
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
  // 任务会话内容跟随：调度器执行无 SSE 通道，靠 /history 定期轮询显现新产出
  const historyQ = useSessionHistory(
    sessionId || null,
    sessionQ.data?.kind === 'cron' ? 5_000 : undefined,
  )
  const archiveSession = useArchiveSession()
  const { modal } = AntApp.useApp()
  // 会话数据上提：handleWithdraw（useCallback）需在早退分支之前定义（Hook 顺序稳定），
  // 引用随查询数据变化（React Query 结构共享，未变即同引用）
  const session = sessionQ.data

  // 任务会话浏览态数据源：绑定任务行（last_status 驱动「任务运行中」徽标与
  // HITL 重建信号）。非任务会话禁用查询（不产生请求）。
  const cronJobs = useCronJobs(session?.kind === 'cron', 5_000)
  const cronJob = session?.kind === 'cron'
    ? cronJobs.data?.find((j) => j.session_id === sessionId)
    : undefined
  const cronRunning = cronJob?.last_status === 'running'

  // 观察流仅普通会话开启：任务会话（cron）保持 /history 轮询跟随（行为不变），
  // 归档会话不可能有 run。观察流与 /history 并行发起（重叠 RTT），事件分发
  // 等历史灌入完成后按序冲刷（水合门在 hook 内，settleHydration 由下方灌入
  // effect 调用；观察循环自身也会等水合后再判定会话绑定——冷进入竞态修复）
  const observerEligible = session?.kind === 'chat' && session?.status !== 'archived'
  const stream = useStreamChat(sessionId || null, observerEligible)
  const settleHydration = stream.settleHydration
  const capabilities = useCapabilities()
  // —— 会话-智能体解耦（§4.4）：选中智能体 = chatStore 纯 UI 态 ——
  const agentsQ = useAgents()
  /** 可选智能体（active + chat 型）：composer 选择器数据源 */
  const chatAgents = (agentsQ.data ?? []).filter(
    (a) => (a.kind ?? 'chat') === 'chat' && a.status === 'active',
  )
  const selectedAgentId = useChatStore((s) => s.selectedAgentId)
  const setSelectedAgentId = useChatStore((s) => s.setSelectedAgentId)
  /** last 可解析（agent 仍在活跃列表）：HITL 悬空期门禁的解锁判据——
   *  last 悬空（agent 已删/归档）时保持可选，为"选中新智能体回撤悬空回合"
   *  留逃生路径（后端 send 仍 409，仅放行 withdraw 取图） */
  const lastAgentResolvable =
    !session?.last_agent_id || chatAgents.some((a) => a.id === session?.last_agent_id)
  const agentLocked =
    stream.status === 'streaming' || (stream.status === 'interrupted' && lastAgentResolvable)
  const agentLockedReason =
    stream.status === 'streaming'
      ? '回合生成中，结束后可切换智能体'
      : '存在待审批中断，处理完毕前不可切换智能体'
  // 语音输入：仅后端能力开启时预建 WS（与按键解耦）；推理闸门复用 stream.status。
  // 预建连仅限可发言会话：任务会话（kind=cron）与归档会话会被后端 WS 守卫以
  // error 帧拒绝（经共享 dispatch 落为「会话出错」横幅）——在源头即不连接。
  const voice = useVoiceInput(
    sessionId || null,
    capabilities.voice_enabled && session?.kind === 'chat' && session?.status !== 'archived',
  )

  /** 中止当前流（按来源路由，语音回合无法中止/回撤的冲突修复点）：
      语音发起 → WS cancel 帧（后端取消 run 并广播 canceled 终态）；SSE 发起 →
      POST /cancel + 本地落态兜底（run 释放会话闸，withdraw 即刻放行）。两条路径
      随后统一本地固化半截消息并回 idle（语义一致，canceled 事件幂等）。
      回调保持引用稳定（依赖的是两个稳定句柄而非整个 voice/stream 对象）：
      该函数经 props 传入 memo 化的 MessageList/气泡，引用漂移会击穿渲染隔离。 */
  const voiceCancelStream = voice.cancelStream
  const streamCancel = stream.cancel
  const handleCancel = useCallback(() => {
    const st = useChatStore.getState()
    if (st.status === 'streaming' && st.streamSource === 'voice') voiceCancelStream()
    streamCancel()
  }, [voiceCancelStream, streamCancel])
  const wsCapQ = useWorkspaceCapability(sessionId || null, selectedAgentId)
  const allowWorkspace = wsCapQ.data?.allow_workspace === true
  const shellCapQ = useShellCapability(sessionId || null, selectedAgentId)
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
  const setStreamError = useChatStore((s) => s.setStreamError)
  // 回撤所需 store 动作
  const removeTurn = useChatStore((s) => s.removeTurn)
  const setDraft = useChatStore((s) => s.setDraft)
  const setAttachments = useChatStore((s) => s.setAttachments)
  const setKbs = useChatStore((s) => s.setKbs)
  const clearInterrupt = useChatStore((s) => s.clearInterrupt)
  const setStatus = useChatStore((s) => s.setStatus)
  const [withdrawingId, setWithdrawingId] = useState<string | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  // 是否"贴底"：用户没主动上滑翻历史。发消息/新条目出现时强制置 true。
  const pinnedRef = useRef(true)
  // 跳底悬浮钮可见态（DeepSeek 式）：离底超过贴底阈值即显现。与 pinnedRef
  // 分离——ref 不触发渲染，按钮显隐需要状态。sessionId 作用域（同 wsState
  // 模式）：跨会话派生回隐没，无需 effect 复位；函数式更新同值复用旧引用，
  // 逐 scroll 事件调用无额外渲染开销
  const [jumpState, setJumpState] = useState<{ sessionId: string; visible: boolean }>({
    sessionId,
    visible: false,
  })
  const jumpVisible = jumpState.sessionId === sessionId && jumpState.visible
  // 指针是否悬停在消息流内：流式期间悬停时暂停"追底"滚动——否则持续增长的
  // 内容会把"停止并回撤"等悬浮操作按钮推离光标，hover 断链、根本点不中。
  const hoverRef = useRef(false)

  // —— 可拖拽布局状态 ——
  // 输入区高度（纵向拖拽，作为 composer 最小高度）。默认 null=贴合内容（短提示不显瘦高），
  // 拖拽后写入地板值。消息流保持居中阅读列。
  const [inputHeight, setInputHeight] = useState<number | null>(null)

  // —— 发送框随滚动收起（走查调整：只要滚动即收起）——
  // 用户任意方向滚动会话 → 收起为 sliver，阅读空间真实释放（收起 = transform
  // 沉降 + --composer-h 同步收缩）；悬停 sliver / 聚焦 / 单击手柄 / 贴底
  // 恒展开。程序化滚动（流式追底/跳底）静默豁免——那是机器跟随而非阅读意图；
  // 逐 scroll 事件累积位移超阈值（28px）才切换，滚轮动量尾/微抖不误收；
  // 聚焦输入（正在编写）锁定展开，失焦后等下一次滚动意图再收（不做自动猜心）。
  const [dockState, setDockState] = useState<{ sessionId: string; collapsed: boolean }>({
    sessionId,
    collapsed: false,
  })
  const dockCollapsed = dockState.sessionId === sessionId && dockState.collapsed
  const dockCollapsedRef = useRef(false)
  const applyComposerHRef = useRef<(() => void) | null>(null)
  /** 上次 scroll 位置与位移累积量（会话切换时一并复位） */
  const prevTopRef = useRef(0)
  const dockAccRef = useRef(0)
  /** 程序化滚动静默截止时刻：scrollToBottom / 跳底前主动写入，窗口内的
      scroll 事件不参与收起判定（流式追底是机器跟随，不代表用户阅读意图） */
  const dockMuteUntilRef = useRef(0)
  /** 焦点在 composer 内（onFocus/onBlur 冒泡到 dock 维护）：编写中不收起 */
  const dockFocusRef = useRef(false)
  useEffect(() => {
    dockCollapsedRef.current = dockCollapsed
    applyComposerHRef.current?.()
  }, [dockCollapsed])

  const onInputResizeDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    const el = e.currentTarget as HTMLElement
    const startY = e.clientY
    // 基线对齐加高后的 composer 默认高度（minRows 3 ≈ 140px），首次拖拽不起跳
    const startH = inputHeight ?? 140
    let moved = false
    const onMove = (ev: MouseEvent) => {
      moved = true
      setInputHeight(clamp(88, 380, startH + (startY - ev.clientY)))
    }
    const onUp = () => {
      el.removeAttribute('data-resizing')
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
      // 收起态下"无位移单击拖拽条" = 展开发送框（sliver 的显式唤起手势）
      if (!moved && dockCollapsedRef.current) {
        setDockState((cur) => (cur.sessionId === sessionId ? { ...cur, collapsed: false } : cur))
      }
    }
    el.setAttribute('data-resizing', '')
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'row-resize'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  // —— 玻璃 dock 高度 → --composer-h ——
  // composer 悬浮于滚动区之上（流体玻璃取景消息流），高度随 chips/textarea
  // 自动增高/拖拽实时变化；ResizeObserver 把 dock 实际高度写到 body 的 CSS 变量，
  // 阅读列 padding-bottom 引用它让位，最后一条消息永不沉入玻璃下方不可读。
  // callback ref 模式：archived 切换引起的 dock 挂载/卸载自动接好观察与清理。
  const dockRoRef = useRef<ResizeObserver | null>(null)
  const dockBodyRef = useRef<HTMLElement | null>(null)
  const attachDock = useCallback((el: HTMLDivElement | null) => {
    dockRoRef.current?.disconnect()
    dockRoRef.current = null
    // dock 卸载（如切到归档会话）时清掉写死在 body 上的让位变量：否则残留的
    // 内联值会盖过 .chat-panel--archived 定义、经继承生效的 0px
    dockBodyRef.current?.style.removeProperty('--composer-h')
    dockBodyRef.current = null
    applyComposerHRef.current = null
    const body = el?.parentElement
    if (!el || !body || typeof ResizeObserver === 'undefined') return
    dockBodyRef.current = body
    // 收起态写 sliver 高（38px）而非实际高度：dock 沉降后让位量同步收回
    const apply = () =>
      body.style.setProperty('--composer-h', `${dockCollapsedRef.current ? 38 : el.offsetHeight}px`)
    applyComposerHRef.current = apply
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    dockRoRef.current = ro
  }, [])

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    // 程序化滚动：静默收起判定一个窗口（追底/回底是机器行为，不算滚动意图）
    dockMuteUntilRef.current = Date.now() + 800
    el.scrollTop = el.scrollHeight
  }, [])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const top = el.scrollTop
    const pinned = el.scrollHeight - top - el.clientHeight < 96
    pinnedRef.current = pinned
    // —— 发送框收起意图（只要滚动即收起，详见 dockState 声明处注释）——
    // 不论方向：用户滚动累积 28px 即收起；贴底恒展开；程序化滚动（静默窗口
    // 内）豁免；正在编写（焦点在 composer 内）不收起。
    const dy = top - prevTopRef.current
    prevTopRef.current = top
    if (pinned) {
      dockAccRef.current = 0
      if (dockCollapsedRef.current) {
        setDockState((cur) => (cur.sessionId === sessionId ? { ...cur, collapsed: false } : cur))
      }
    } else if (Date.now() < dockMuteUntilRef.current) {
      // 程序化滚动进行中（追底/跳底动画）：重置累积，不视为阅读意图
      dockAccRef.current = 0
    } else if (Math.abs(dy) >= 2) {
      dockAccRef.current += Math.abs(dy)
      if (dockAccRef.current >= 28) {
        dockAccRef.current = 0
        // 正在编写（焦点在 composer 内）不收起
        if (dockCollapsedRef.current === false && !dockFocusRef.current) {
          setDockState((cur) => (cur.sessionId === sessionId ? { ...cur, collapsed: true } : cur))
        }
      }
    }
    setJumpState((cur) =>
      cur.sessionId === sessionId && cur.visible === !pinned ? cur : { sessionId, visible: !pinned },
    )
  }, [sessionId])

  /** 跳底钮点击：平滑滚回最新一条。pinnedRef 立即置真——流式追底（store 订阅 /
      ResizeObserver）在动画期间即重新接管，长动画不必等落底才恢复跟随；
      按钮可见性由随后到达底部的 scroll 事件自然收敛隐藏 */
  const jumpToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    pinnedRef.current = true
    // 平滑回底动画期间的 scroll 事件均为程序化：静默收起判定
    dockMuteUntilRef.current = Date.now() + 800
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
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
  // 跳过 reset + 历史灌入——store 含 SSE 实时增量与 todos/文件变更等 /history
  // 不含的富视图状态，任何重灌都是不可逆降级（这正是"切页返回后部分上下文与
  // 回合标签消失"的根因）。工具卡虽可由 history 重建（buildHistoryReplay），
  // 但保留模式下 live 状态本就完整，同样无需重灌。
  const retainedRef = useRef(false)
  // 显式失效灌入（withdraw 后的服务端真值兜底）：true 时放行一次 loadHistory
  const forcedLoadRef = useRef(false)
  // 失效发起时刻：只接受此后完成的新快照，防 refetch 失败时把撤回前的旧缓存灌回
  const forcedAtRef = useRef(0)
  // 任务会话内容跟随：上次已灌入快照的 dataUpdatedAt（0=仅记录基线未灌入）
  const lastCronLoadAtRef = useRef(0)

  // 会话切换（或首次进入未绑定的会话）：重置 store；同会话往返：保留 live 状态
  useEffect(() => {
    retainedRef.current = useChatStore.getState().sessionId === sessionId
    forcedLoadRef.current = false
    forcedAtRef.current = 0
    lastCronLoadAtRef.current = 0
    // 发送框滚动意图基准复位：旧会话的 scrollTop 不参与新会话首个 dy 计算
    prevTopRef.current = 0
    dockAccRef.current = 0
    dockMuteUntilRef.current = 0
    if (!retainedRef.current) {
      // 跨会话进入：拆除上一会话的本地消费连接（防旧流事件串进新会话的
      // store）——服务端 run 不受影响，重进原会话经观察流补帧
      detachLiveConsumers()
      reset()
    }
  }, [sessionId, reset])

  // —— 选中智能体初始化（§4.4）——
  // 选中是纯 UI 态（reset 清空、刷新即失）：进入会话时从 session.last_agent_id
  // 兜底初始化（后端在 send 时写入的最新真值）；非空不覆写——同会话内用户已
  // 切换的选择在 session 重拉 / 回撤重灌后保持不变（loadHistory 不触碰该字段）。
  useEffect(() => {
    if (!session) return
    if (useChatStore.getState().selectedAgentId === null) {
      setSelectedAgentId(session.last_agent_id ?? null)
    }
  }, [session, setSelectedAgentId])

  // —— 待审批恢复（HITL 重入重建）——
  // 待决 interrupt 只存活于 live SSE 流的前端内存态：刷新/切会话即丢失审批卡，
  // 而图仍暂停在 interrupt（工具卡永挂「执行中」，发新消息还会静默取消待审批）。
  // 会话进入时向 checkpointer 查询真值，有待审批则重建 interrupted 状态与审批卡
  // （准则 F3：终态必须落定且可恢复）。live 流自己的 interrupt 已写入 store，
  // 下方守卫（非 idle / 已有 pendingInterrupt）确保不覆盖。
  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    const agentId = useChatStore.getState().selectedAgentId
    void (async () => {
      try {
        const payload = await streamApi.getInterrupt(sessionId, agentId)
        if (cancelled || !payload?.action_requests?.length) return
        const store = useChatStore.getState()
        if (store.status !== 'idle' || store.pendingInterrupt) return
        store.setInterrupt(payload)
      } catch {
        /* 恢复失败不阻塞会话进入（如模型不可用 409），退化为旧行为 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId])

  // —— 回撤 ——
  // useCallback + ref 闸门（而非依赖 withdrawingId state）：引用稳定，不击穿
  // MessageList/气泡的 memo 隔离；并发防抖语义与原 state 闸门一致
  const withdrawingRef = useRef(false)
  const refetchHistory = historyQ.refetch
  /** 回撤一个完整回合：① 流式中先中止（SSE abort / 语音 WS cancel，后端随后
      释放 tracker）→ ② 解析真实 id → ③ 调回撤（409 退避重试）→ ④ 乐观移除 +
      回填草稿/附件 + 退出中断态 → ⑤ 历史重拉兜底一致性。 */
  const handleWithdraw = useCallback(
    async (messageId: string) => {
      if (!session || withdrawingRef.current) return
      // 场景①：流式中先停止生成（按来源路由中止 + 置 idle），后端释放 tracker
      if (useChatStore.getState().status === 'streaming') handleCancel()
      withdrawingRef.current = true
      setWithdrawingId(messageId)
      try {
        const realId = await resolveHumanId(session.id, messageId)
        // 弱绑定取图（§4.4）：显式传当前选中智能体（恒传——UI 所选即执行图），
        // 后端按"显式 > last"解析；last 悬空时无需先发消息即可回撤
        const res = await callWithdraw(
          session.id,
          realId,
          useChatStore.getState().selectedAgentId,
        )
        removeTurn(messageId)
        setDraft(res.raw_content)
        setAttachments(res.attachments ?? [])
        setKbs(res.kbs ?? [])
        clearInterrupt()
        setStatus('idle')
        toastSuccess('已回撤该回合，可编辑后重发')
        // 历史失效重拉：以服务端真值兜底（准则 R9），覆盖乐观删除的边界情况。
        // 同会话保留模式下灌入被跳过，必须显式标记"放行一次"，且只接受本次
        // 请求之后完成的新快照（refetch 失败时不得把撤回前的旧缓存灌回）。
        forcedAtRef.current = Date.now()
        forcedLoadRef.current = true
        void refetchHistory()
      } catch (e) {
        toastError(e instanceof ApiError ? e.message : '回撤失败，请重试')
      } finally {
        withdrawingRef.current = false
        setWithdrawingId(null)
      }
    },
    [
      session,
      handleCancel,
      removeTurn,
      setDraft,
      setAttachments,
      setKbs,
      clearInterrupt,
      setStatus,
      refetchHistory,
    ],
  )

  // 历史灌入：仅在数据就绪（非 fetching）时灌入。配合 useSessionHistory 的
  // staleTime:0，跨会话进入都会先 refetch 最新；refetch 进行中不灌入，避免用过时
  // 快照覆盖刚被 reset() 清空的 store。
  // 关键防护：历史"从非空突变为空"几乎只可能是后端异常快照（中断流收尾瞬间
  // _load_messages 的 delta 重建偶发返回 []），拒绝用它抹白 store——这正是"中止后重进、
  // 历史全空"的直接诱因。以 store 现存消息数为准（跨会话首灌时 store 已被 reset 清空，
  // 空快照照常灌入）。React Query 出错时保留上次 data，故此处仍能灌入兜底历史。
  useEffect(() => {
    // 同会话往返且未被显式失效：store 即最新真值，跳过一切灌入（水合即完成）
    if (retainedRef.current && !forcedLoadRef.current) {
      settleHydration()
      return
    }
    // 显式失效：只接受失效之后完成的新快照（refetch 失败时 dataUpdatedAt 不变）
    if (forcedLoadRef.current && historyQ.dataUpdatedAt <= forcedAtRef.current) return
    // 流式进行中禁止灌入：服务端快照在节点完成前不含 token 级部分输出，
    // 此时 loadHistory 会抹掉正在增长的流式气泡并把 status 强置回 idle。
    // 待审批（interrupted）同样禁止：快照灌入会冲掉刚写入的审批卡（loadHistory
    // 已保留 pendingInterrupt，此处守卫双保险；回撤路径会先 clearInterrupt+idle）
    const stNow = useChatStore.getState()
    if (stNow.status === 'streaming' || stNow.status === 'interrupted') {
      // 冷进入的中断恢复先到（罕见时序）：历史灌入被拦即降级放行水合门，
      // 重放照常冲刷（仅当前回合可见，可接受）
      settleHydration()
      return
    }
    // 回撤兜底快照只对"回撤发起时刻之前"的本地状态负责：若用户已编辑重发
    // （forcedAt 之后又出现新消息），迟到的快照会整体抹掉新回合——正是
    // "回撤后重发、query 不在会话末尾"的诱因。放弃本次兜底（乐观删除已生效）。
    if (forcedLoadRef.current) {
      const st = useChatStore.getState()
      if (st.messages.some((m) => m.createdAt > forcedAtRef.current)) {
        forcedLoadRef.current = false
        settleHydration()
        return
      }
    }
    if (!historyQ.data || historyQ.isFetching) return
    if (historyQ.data.length === 0 && useChatStore.getState().messages.length > 0) {
      // store 已有本地基底（空快照不可抹白）：水合视为完成
      settleHydration()
      return
    }
    // 消息 + 工具卡一次重建：AI 消息的 tool_calls 与 ToolMessage 的结果配对还原，
    // 刷新 / 重进 / 归档会话均可回看工具调用细节（映射细节见 historyReplay.ts）
    const { messages, toolCalls } = buildHistoryReplay(historyQ.data)
    loadHistory(sessionId, messages, toolCalls)
    forcedLoadRef.current = false
    // 历史基底已灌入：解除水合门，观察流缓冲的事件按序冲刷转 live
    settleHydration()
    // 冷进入兜底重挂：观察流 effect 首轮可能在灌入绑定前已退出（绑定守卫
    // 竞态的水合等待超时路径等边角），此刻 store 已绑定本会话，幂等补挂
    // 一次——同会话已有观察流则内部跳过，不产生双路分发
    if (observerEligible) ensureObserver(sessionId)
  }, [sessionId, observerEligible, historyQ.data, historyQ.dataUpdatedAt, historyQ.isFetching, loadHistory, settleHydration])

  // —— 水合门兜底（配合 hook 内的超时）：历史查询失败时放行，降级为仅重放
  // 内容可见（可接受）——
  useEffect(() => {
    if (historyQ.isError) settleHydration()
  }, [historyQ.isError, settleHydration])

  // —— 任务会话内容跟随（cron live-follow）——
  // 调度器执行无 SSE sink（后端 cron_scheduler._invoke 直接消费事件流），浏览侧
  // 无流式事件可收：不轮询则整个运行期间页面静止在进入时刻的快照。上方
  // useSessionHistory 对任务会话已按 5s 轮询，此处把"新到达的快照"重灌进 store
  // （checkpointer 以节点完成为粒度落盘——回复/工具卡逐步显现，非 token 级流式）。
  // 同会话保留守卫（retainedRef）服务于 live 流场景，任务会话无本地流，重灌安全。
  useEffect(() => {
    if (!sessionId || session?.kind !== 'cron') return
    if (!historyQ.data || historyQ.isFetching) return
    if (lastCronLoadAtRef.current === 0) {
      // 首个快照由上方挂载灌入路径负责，这里只记录基线时刻
      lastCronLoadAtRef.current = historyQ.dataUpdatedAt
      return
    }
    if (historyQ.dataUpdatedAt <= lastCronLoadAtRef.current) return
    const st = useChatStore.getState()
    if (st.sessionId !== sessionId) return
    // 本地流式/待审批中不灌入（语义同上方挂载灌入守卫）；空快照不抹白
    if (st.status === 'streaming' || st.status === 'interrupted') return
    if (historyQ.data.length === 0) return
    lastCronLoadAtRef.current = historyQ.dataUpdatedAt
    const { messages, toolCalls } = buildHistoryReplay(historyQ.data)
    loadHistory(sessionId, messages, toolCalls)
    // 用量徽标以服务端计数落真值（usage 事件后端已即时入库）；工作区已挂载则
    // 同步文件树（调度器落盘产物即时可见）
    queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) })
    if (wsVisited) {
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaceTree(sessionId) })
    }
  }, [
    sessionId,
    session?.kind,
    historyQ.data,
    historyQ.dataUpdatedAt,
    historyQ.isFetching,
    loadHistory,
    queryClient,
    wsVisited,
  ])

  // —— 任务会话浏览中命中 HITL：审批卡重建 ——
  // 待审批状态本只存活于 live SSE 流（interrupt 事件）；调度器运行无此通道，
  // 浏览中的用户不会收到 interrupt 事件。以任务状态 awaiting_approval 为信号，
  // 补查 checkpointer 真值（GET /interrupt）重建审批卡（复用会话进入路径的守卫）。
  useEffect(() => {
    if (!sessionId || session?.kind !== 'cron') return
    if (cronJob?.last_status !== 'awaiting_approval') return
    const st = useChatStore.getState()
    if (st.status !== 'idle' || st.pendingInterrupt) return
    let cancelled = false
    const agentId = useChatStore.getState().selectedAgentId
    void (async () => {
      try {
        const payload = await streamApi.getInterrupt(sessionId, agentId)
        if (cancelled || !payload?.action_requests?.length) return
        const cur = useChatStore.getState()
        if (cur.status !== 'idle' || cur.pendingInterrupt) return
        cur.setInterrupt(payload)
      } catch {
        /* 查询失败退化为下次状态变化再试（任务状态轮询会再次触发） */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId, session?.kind, cronJob?.last_status])

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
    if (prev === 'streaming' && stream.status !== 'streaming') {
      if (wsVisited) {
        queryClient.invalidateQueries({ queryKey: queryKeys.workspaceTree(sessionId) })
      }
      // token 累计真值兜底：usage 事件已逐条入库，重拉 session 让基线同步效应
      // 以服务端计数替换本地累加（校正丢帧/本地漏算的偏差）
      queryClient.invalidateQueries({ queryKey: queryKeys.session(sessionId) })
    }
  }, [stream.status, scrollToBottom, wsVisited, queryClient, sessionId])

  // token 用量基线同步：session 查询的 usage_totals → store（整表替换）。
  // 流式中跳过——服务端快照必然滞后于事件增量（usage 先于 done 入库），
  // 此时套用会把正在跳动的徽标拉回旧值；终态失效重拉后本效应自然落真值。
  const setUsageTotals = useChatStore((s) => s.setUsageTotals)
  const usageBaseline = sessionQ.data?.usage_totals
  useEffect(() => {
    if (!usageBaseline || useChatStore.getState().status === 'streaming') return
    setUsageTotals(usageBaseline)
  }, [usageBaseline, setUsageTotals])

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

  // —— 贴底鲁棒化（内容高度沉降跟随）——
  // 历史灌入后的单次 scrollTop=scrollHeight 会败给异步撑高：mermaid/KaTeX/
  // 图片在滚动之后才落定高度，把最新一轮推到视口下方（进入会话不在最底的
  // 根因）。ResizeObserver 观察阅读列实际高度，贴底且未悬停期间一变即追底，
  // 高度稳定后自然静止；上滑翻历史（pinnedRef=false）或悬停瞄操作时零打扰。
  // rAF 合帧：连续撑高（流式/多图加载）至多排队一次滚动，与上方 store 订阅
  // 互为兜底（滚动幂等，重复触发无害）。依赖 session?.id：加载完成/切换会话
  // 时滚动 DOM 才挂载，届时重绑观察。
  useEffect(() => {
    const column = scrollRef.current?.querySelector<HTMLElement>('.chat-panel__column')
    if (!column || typeof ResizeObserver === 'undefined') return
    let raf = 0
    const ro = new ResizeObserver(() => {
      if (!pinnedRef.current || hoverRef.current) return
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(scrollToBottom)
    })
    ro.observe(column)
    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
    }
  }, [session?.id, scrollToBottom])

  // 页面标题/动作上提顶部 Header 卡（与列表页一致的模块化布局）。
  // 每次渲染重注册：状态 Tag（生成中/待审批）与动作回调闭包保持新鲜；
  // 早退分支（加载中）也会注册，标题用短 id 兜底。
  useEffect(() => {
    const session = sessionQ.data
    const archived = session?.status === 'archived'
    const isCron = session?.kind === 'cron'
    // 任务会话与归档会话同态只读（composer 隐藏）；但任务会话保留工作区切换与
    // HITL 审批入口（产出浏览与审批是只读/放行交互），归档则全收
    const readOnly = archived || isCron
    const effView: ChatView = allowWorkspace && !archived ? wsView : 'chat'
    setHeader({
      title: (
        <Space>
          <Button icon={<ArrowLeft size={14} />} onClick={() => navigate(isCron ? '/cron' : '/sessions')}>
            返回
          </Button>
          <Typography.Text strong>{session?.title || `会话 ${sessionId.slice(0, 8)}`}</Typography.Text>
          {session?.agent_name ? <Tag color="purple">{session.agent_name}</Tag> : null}
          {isCron ? <Tag color="geekblue">定时任务·只读</Tag> : null}
          {isCron && cronRunning ? <Tag color="processing">任务运行中</Tag> : null}
          {allowShell ? <Tag color="magenta">Shell</Tag> : null}
          {stream.status === 'streaming' ? <Tag color="processing">生成中</Tag> : null}
          {stream.status === 'interrupted' ? <Tag color="orange">待审批</Tag> : null}
          {/* 会话级 token 用量（含缓存命中）：元信息层，流中实时跳动，Tooltip 见明细。
              定时任务会话不展示：每轮线程重置（无历史上下文），跨轮累计值无参考意义 */}
          {isCron ? null : <UsageBadge />}
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
          {!readOnly ? (
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
          ) : null}
        </Space>
      ),
    })
    return () => setHeader(null)
  })

  if (sessionQ.isLoading) return <PageLoading tip="加载会话…" />
  if (!session) {
    return (
      <Alert
        type="warning"
        message="会话不存在"
        action={<Button onClick={() => navigate('/sessions')}>返回列表</Button>}
      />
    )
  }

  const archived = session.status === 'archived'
  const isCron = session.kind === 'cron'
  // 只读 = 归档或任务会话（composer 隐藏、回撤禁用）；工作区切换仅归档时收起
  //（任务会话保留产出浏览），HITL 审批卡仅归档时隐藏（任务会话审批是放行交互）
  const readOnly = archived || isCron
  const effectiveView: ChatView = allowWorkspace && !archived ? wsView : 'chat'

  return (
    <Flex vertical gap={0} style={{ height: '100%' }}>
      {/* 顶部（标题/状态/切换/归档已上提 Header 卡，见上方 setHeader effect） */}

      {/* 错误提示（仅出错时出现，不占常规布局）。关闭横幅 = 复位错误态：
          setStreamError(null) 连带 status 回 idle——否则 error 态会锁死发送
          （历史缺陷：仅视觉隐藏时状态机永不恢复） */}
      {streamError ? (
        <Alert
          type="error"
          message="会话出错"
          description={streamError}
          closable
          onClose={() => setStreamError(null)}
          style={{ margin: '0 0 8px' }}
        />
      ) : null}

      {/* 主体：页内双视图（会话 | 工作区）display 切换保活 —— 切到工作区时聊天面板
          不卸载，SSE 流与滚动位置不中断/不丢失。会话区包装为独立模块卡片（chat-panel），
          消息流与输入框之间的分隔条内化为卡片内部，默认隐形。 */}
      <Flex gap={12} style={{ flex: 1, minHeight: 0 }}>
        <section
          className={readOnly ? 'chat-panel chat-panel--archived' : 'chat-panel'}
          style={{ display: effectiveView === 'chat' ? 'flex' : 'none' }}
        >
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
                  onWithdraw={isCron ? undefined : handleWithdraw}
                  withdrawingId={withdrawingId}
                  readOnly={readOnly}
                  onOpenFile={allowWorkspace ? openWorkspaceFile : undefined}
                />
                {!archived ? <HitlCard resume={stream.resume} /> : null}
              </div>
              </div>
              {/* 回合导航轨：首个回合落定即渲染（流式/待审批中的末轮暂不入轨）；
                  形态随面板宽度分级（容器查询，见 index.css）。
                  key=sessionId：同路由切换会话时重挂复位折叠态（TurnRail 内部
                  useState 不随 store reset 清空） */}
              <TurnRail key={sessionId} scrollRef={scrollRef} />

              {/* 跳底悬浮钮（DeepSeek 式）：上滑翻历史 / TurnRail 跳转旧轮次后
                  浮现于发送框右上角（右缘对齐 composer），点击平滑回最新；
                  贴底时隐没（data-show 驱动，隐藏态保留 DOM 以获得双向过渡） */}
              <button
                type="button"
                className="jump-bottom-btn"
                data-show={jumpVisible ? '' : undefined}
                onClick={jumpToBottom}
                aria-label="跳转至最新消息"
                title="跳转至最新消息"
              >
                <ChevronDown size={17} />
              </button>

              {/* 玻璃 dock：composer 悬浮于滚动区底部（流体玻璃取景消息流，
                  发送后消息从玻璃下滚过、模糊可见）；dock 本身不拦截指针，
                  宽屏两侧空白处可正常 hover/点击下方消息。
                  滚动收起（--collapsed）：用户任意方向滚动会话即沉降为 sliver（玻璃
                  上缘 + grip 手柄）；悬停/聚焦临时展开，贴底恒展开，单击手柄唤起。
                  归档/任务会话不渲染（只读，发送窗由下方常规流 Alert 替代） */}
              {!readOnly ? (
                <div
                  className={dockCollapsed ? 'chat-panel__dock chat-panel__dock--collapsed' : 'chat-panel__dock'}
                  ref={attachDock}
                  onFocus={() => {
                    // 聚焦（点击输入区打字）= 编写中：锁定展开（状态 + 视觉一致）
                    dockFocusRef.current = true
                    if (dockCollapsedRef.current) {
                      setDockState((cur) => (cur.sessionId === sessionId ? { ...cur, collapsed: false } : cur))
                    }
                  }}
                  onBlur={(e) => {
                    // 焦点移出 composer 才解锁（dock 内部控件间切换不触发收起判定）
                    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) dockFocusRef.current = false
                  }}
                >
                  {/* 高度手柄：全宽隐形热区骑跨玻璃上缘，视觉重心为居中 grip 胶囊
                      （与 DragTextarea 抓手同语言）；单击收起态=展开，双击=高度复位 */}
                  <div
                    className="composer-resize"
                    onMouseDown={onInputResizeDown}
                    onDoubleClick={() => setInputHeight(null)}
                    title="拖拽调整输入区高度（双击复位）"
                  >
                    <span className="composer-resize__grip" />
                  </div>
                  <div className="chat-panel__composer" style={{ minHeight: inputHeight ?? undefined }}>
                    <ChatInput
                      sessionId={session.id}
                      status={stream.status}
                      send={stream.send}
                      cancel={handleCancel}
                      voice={voice}
                      agents={chatAgents}
                      selectedAgentId={selectedAgentId}
                      onSelectAgent={setSelectedAgentId}
                      agentLocked={agentLocked}
                      agentLockedReason={agentLockedReason}
                    />
                  </div>
                </div>
              ) : null}
            </div>

            {/* 输入：归档/任务会话只读，不可发送（常规流布局，不参与玻璃悬浮）。
                任务会话保留 HITL 审批能力说明（审批卡在消息流内，resume 已放行） */}
            {readOnly ? (
              <div className="chat-panel__composer">
                {isCron ? (
                  <Alert
                    type="info"
                    message={
                      cronRunning
                        ? '任务正在后台运行，产出将以节点完成粒度自动刷新至本页（非逐字流式）；仅支持查看运行产出与 HITL 审批。'
                        : '该会话由定时任务驱动，仅支持查看运行产出与 HITL 审批，不支持手动发送。'
                    }
                  />
                ) : (
                  <Alert type="info" message="该会话已归档，仅支持查看历史记录，不可继续对话。" />
                )}
              </div>
            ) : null}
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
