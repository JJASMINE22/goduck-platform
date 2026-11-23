/* 聊天 SSE 状态机 hook（DESIGN §8 核心）—— 回合与连接解耦版（sse-run-detach-reattach）。
   职责：把后端 SSE 事件流映射到 chatStore 动作；维护 idle/streaming/interrupted/error。
   - send：发新消息（仅 idle 可用）；resume：HITL 决策恢复（仅 interrupted 可用）；
   - cancel：显式取消（POST /cancel + 本地落态兜底，镜像手动中止语义）；
   - 观察流（GET /sessions/{id}/stream）：重进会话 / POST 断连后的补帧通道。
   终态仅由 done/interrupt/error/canceled 决定（准则 F3）。

   连接生命周期（核心行为变化）：
   - POST fetch 与 GET 观察流登记在模块级（跨组件卸载存活）——离开页面只关
     "观察窗口"，服务端 run 后台续跑，重进经观察流补帧转 live；
   - 切换会话时统一拆除本地消费连接（detachLiveConsumers，防跨会话串流），
     服务端 run 仍不受影响；
   - POST 断连（网络异常 / 后端硬杀）：先尝试观察流接续（run 可能仍在执行），
     204 确认无 run 才回落本地截断（旧行为）。 */
import { useCallback, useEffect, useRef } from 'react'
import { buildHistoryReplay } from '@/features/chat/historyReplay'
import { queueApi } from '@/api/queue'
import { sessionsApi } from '@/api/sessions'
import { streamApi } from '@/api/stream'
import { hydrateFileChanges } from '@/lib/fileChanges'
import { useChatStore } from '@/store/chatStore'
import { dispatchStreamEvent, flushPendingTokens } from '@/lib/streamEvents'
import { SseRejectedError, type StreamTermination } from '@/lib/sse'
import type { ChatStreamStatus, Decision, QueuedItem, SSEEvent, UUID } from '@/types'

export interface SendOptions {
  rubric?: string
}

export interface UseStreamChat {
  status: ChatStreamStatus
  send: (content: string, opts?: SendOptions) => Promise<void>
  resume: (decisions: Decision[]) => Promise<void>
  cancel: () => void
  /** 解除水合门（ChatPage 历史灌入完成后调用；重复调用幂等） */
  settleHydration: () => void
}

// —— 模块级 live 连接登记（跨卸载存活；同会话至多一路消费，防双路重复分发）——
let livePost: { sessionId: string; controller: AbortController } | null = null
let liveObserver: { sessionId: string; controller: AbortController } | null = null

function abortLivePost(sessionId: string): void {
  if (livePost && livePost.sessionId === sessionId) {
    livePost.controller.abort()
    livePost = null
  }
}

function stopObserver(sessionId: string): void {
  if (liveObserver && liveObserver.sessionId === sessionId) {
    liveObserver.controller.abort()
    liveObserver = null
  }
}

/** 拆除全部本地消费连接（跨会话切换时由 ChatPage 调用；服务端 run 不受影响）。 */
export function detachLiveConsumers(): void {
  livePost?.controller.abort()
  livePost = null
  liveObserver?.controller.abort()
  liveObserver = null
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))
const OBSERVER_MAX_RETRIES = 5

/** 水合门兜底时限：正常路径（历史灌入/往返跳过）远早于此 settle */
const HYDRATION_FALLBACK_MS = 10_000

/** 排水 204 竞态的退避窗口（queued-send §6）：on_final 的 drain 是异步派发，
 *  observe 可能先于 drain 启动返回 204 —— 队列 active 时短退避重试 */
const QUEUE_DRAIN_RETRIES = 6
const QUEUE_DRAIN_RETRY_MS = 500

/** 观察循环等待历史水合的独立上限：水合门 promise 仅在组件挂载期可 settle
 *  （卸载后无人调 settle、hook 兜底 timer 亦随 effect 清除）——若无独立上限，
 *  孤儿等待会永久占住模块级 liveObserver 槽位，重进会话再也挂不上观察流。
 *  取值略大于 HYDRATION_FALLBACK_MS：正常路径总是水合先到。 */
const OBSERVER_HYDRATION_WAIT_MS = 12_000

/** 可中止的 sleep：等待期内控制器中止即刻唤醒（清除定时器）。观察循环的水合
 *  等待用它——重进会话触发 detachLiveConsumers 时能立即释放槽位，而非等满
 *  超时才让位给新挂载的观察流。 */
function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        resolve()
      },
      { once: true },
    )
  })
}

/** 服务端真值重建（resync 后）：/history 重灌 store（保留待审批/中断态），
 *  并重置观察流水位（旧水位必然落后于已淘汰的缓冲，保留会导致重挂再次
 *  ResyncNeeded 的死循环）→ 后续重挂全量回放。 */
async function reloadFromServer(sessionId: string): Promise<void> {
  const history = await sessionsApi.history(sessionId)
  const { messages, toolCalls } = buildHistoryReplay(history)
  const store = useChatStore.getState()
  if (store.sessionId === sessionId) {
    store.loadHistory(sessionId, messages, toolCalls)
    store.resetWatermark()
    // 文件变更明细随真值重建回放（best-effort，见 lib/fileChanges）
    await hydrateFileChanges(sessionId)
  }
}

/** 队列真值回查（queued-send §6）：仅同步条带与整体态（用户气泡物化由排水
 *  回合 turn_start 首帧确定性驱动，不依赖回查时点）。返回是否仍绑定本会话
 *  （false = 已切换，调用方应停止后续动作）。 */
async function syncQueueView(sessionId: string): Promise<boolean> {
  let view
  try {
    view = await queueApi.getQueue(sessionId)
  } catch {
    return false // 回查失败：保持现状（下一次终态/重进会话再同步）
  }
  const store = useChatStore.getState()
  if (store.sessionId !== sessionId) return false
  store.syncQueue(view)
  return true
}

/**
 * 水合门（B-Tier1）：观察流请求与 /history **并行**发起（重叠 RTT），但事件
 * 分发等历史水合完成后按序冲刷——先缓冲后冲刷，不丢只延后。
 *
 * hydration 为 null（同会话往返 / 断连恢复路径，store 已有基底）时直通；
 * 水合失败或超时由调用方兜底 settle，此处 flush 同样生效（降级为仅当前回合
 * 可见，可接受）。冲刷同步完成且先置标志——单线程下后续到达的事件不会插队。
 */
function hydrationGate(
  onEvent: (e: SSEEvent) => void,
  hydration: Promise<void> | null,
): (e: SSEEvent) => void {
  if (!hydration) return onEvent
  let hydrated = false
  const queue: SSEEvent[] = []
  const flush = () => {
    hydrated = true
    for (const e of queue.splice(0)) onEvent(e)
  }
  hydration.then(flush, flush)
  return (e: SSEEvent) => {
    if (hydrated) onEvent(e)
    else queue.push(e)
  }
}

/**
 * 观察循环：挂观察流直到 run 终态 / 确认无 run（204）。
 * - resync → /history 重建后全量重挂（loadHistory 已清 lastEventId）；
 * - 网络异常 → 有界重试（run 可能仍在服务端执行，观察是只读幂等的）；
 * - entryGuard：挂载路径下本地 POST/语音正在消费时不双路（恢复路径跳过）；
 * - 绑定守卫先等历史水合（"重进会话内容不更新"的修复点）：冷进入（刷新 /
 *   跨会话重进）时 store 尚未绑定目标会话——loadHistory 要等 /history 网络
 *   返回，同步读 sessionId 必不匹配，循环即刻自尽且无人重启：后台 run 仍
 *   在跑，UI 却停在 /history 快照、发送被单写者闸 409。改为先等水合
 *   （gate settle 或独立兜底超时）再判绑定，未绑定窗口被等待自然覆盖。
 * - 队列联动（queued-send §6）：终态或 204 后回查 GET /queue 同步条带（整体态
 *   以服务端视图为准——sending 行存活整轮 run，active 期间持续重挂观察），
 *   追赶服务端自动排水开跑的下一回合（turn_start 首帧物化用户气泡，
 *   create_task 派发竞态由重试窗口覆盖）。
 */
async function runObserverLoop(
  sessionId: string,
  signal: AbortSignal,
  entryGuard: boolean,
  onEvent: (e: SSEEvent) => void,
  hydration: Promise<void> | null,
): Promise<void> {
  if (hydration) {
    // 等水合再判绑定；事件分发侧的水合门（hydrationGate）照旧缓冲按序冲刷，
    // 两处语义独立：门管事件时序，此处置换绑定时序。abortableSleep 保证
    // detach（重进会话/切换）即刻唤醒释放 liveObserver 槽位。
    await Promise.race([hydration, abortableSleep(OBSERVER_HYDRATION_WAIT_MS, signal)])
    if (signal.aborted) return
  }
  const dispatch = hydrationGate(onEvent, hydration)
  let retries = 0
  let drainRetries = 0
  while (!signal.aborted) {
    const store = useChatStore.getState()
    if (store.sessionId !== sessionId) return
    if (entryGuard && store.status === 'streaming') return
    entryGuard = false // 仅首轮生效
    let term: StreamTermination
    try {
      term = await streamApi.observe(
        sessionId,
        { onEvent: dispatch },
        signal,
        store.lastEventId ?? undefined,
      )
      retries = 0
    } catch {
      if (signal.aborted) return
      if (++retries > OBSERVER_MAX_RETRIES) return
      await sleep(1_000)
      continue
    }
    if (signal.aborted) return
    if (term === 'resync') {
      try {
        await reloadFromServer(sessionId)
      } catch {
        return // 重建失败：维持现状（终态或重进会话时再触发）
      }
      continue
    }
    // 终态（done/interrupt/error/canceled）：队列回查同步条带 + 气泡物化；
    // 成功挂上 run 即重置排水重试计数（长队列逐条续跑不受预算约束）
    if (term !== 'closed') {
      drainRetries = 0
      if (!(await syncQueueView(sessionId))) return
    } else if (useChatStore.getState().sessionId !== sessionId) {
      return
    }
    // 队列 active → 退避重挂（终态后自动排水 / 204 后延迟启动同路径覆盖）
    if (useChatStore.getState().queueState === 'active') {
      if (++drainRetries > QUEUE_DRAIN_RETRIES) return
      await abortableSleep(QUEUE_DRAIN_RETRY_MS, signal)
      if (signal.aborted) return
      continue
    }
    // done/interrupt/error/canceled/closed(204)：观察结束
    return
  }
}

function startObserver(
  sessionId: string,
  entryGuard: boolean,
  onEvent: (e: SSEEvent) => void,
  hydration: Promise<void> | null,
): void {
  if (liveObserver && liveObserver.sessionId === sessionId) return
  if (liveObserver) stopObserver(liveObserver.sessionId)
  const controller = new AbortController()
  liveObserver = { sessionId, controller }
  void runObserverLoop(sessionId, controller.signal, entryGuard, onEvent, hydration).finally(() => {
    if (liveObserver?.controller === controller) liveObserver = null
  })
}

/** 冷进入水合完成后的兜底重挂（ChatPage 灌入 effect 末尾调用）：观察流
 *  effect 的首轮可能在历史灌入绑定前即退出（绑定守卫竞态的兜底），灌入
 *  落定后由此幂等补挂——同会话已有等待中/进行中的观察流则跳过，本地
 *  POST/语音消费中由 entryGuard 拦截，不会双路分发。 */
export function ensureObserver(sessionId: string): void {
  startObserver(sessionId, true, (e) => dispatchStreamEvent(e), null)
}

/** 队列操作（[继续]/[重新排队]）后的观察补挂：active 队列的自动排水回合
 *  经观察流消费（终态边沿订阅只覆盖 streaming 起点的情况）。 */
export function watchQueueDrain(sessionId: string): void {
  const s = useChatStore.getState()
  if (s.sessionId !== sessionId || s.queueState !== 'active') return
  startObserver(sessionId, false, (e) => dispatchStreamEvent(e), null)
}

// —— 队列终态联动（queued-send §6）——
// streaming → 终态边沿触发队列回查：覆盖 POST / 观察流 / 语音 WS 三类消费源
//（观察流循环内另有就地续挂，此处 startObserver 遇槽位占用幂等跳过）。
// done → 同步条带 + active 时重挂观察追赶自动排水（turn_start 物化用户气泡）；
// error/interrupt → 同步条带与挂起横幅。in-flight 去重防边沿抖动期间并发回查。
let queueSyncInFlight = false
useChatStore.subscribe((s, prev) => {
  if (s.status === prev.status) return
  if (prev.status !== 'streaming' || s.status === 'streaming') return
  const sessionId = s.sessionId
  if (!sessionId || queueSyncInFlight) return
  queueSyncInFlight = true
  void (async () => {
    try {
      const bound = await syncQueueView(sessionId)
      if (!bound) return
      if (useChatStore.getState().queueState === 'active') {
        startObserver(sessionId, true, (e) => dispatchStreamEvent(e), null)
      }
    } finally {
      queueSyncInFlight = false
    }
  })()
})

/** POST 断连/网络异常后的恢复：先经观察流接续（run 可能仍在执行），
 *  204 确认无 run 才回落本地截断（旧"连接中断"行为，含 HITL 重进可恢复提示）。
 *  恢复路径 store 已有流式基底，无水合门（直通分发）。 */
async function recoverOrTruncate(
  sessionId: string,
  onEvent: (e: SSEEvent) => void,
): Promise<void> {
  const store = useChatStore.getState()
  if (store.sessionId !== sessionId || store.status !== 'streaming') return
  let recovered = false
  try {
    const term = await streamApi.observe(
      sessionId, { onEvent }, undefined, store.lastEventId ?? undefined,
    )
    if (term === 'resync') {
      await reloadFromServer(sessionId)
      startObserver(sessionId, false, onEvent, null)
      recovered = true
    } else if (term !== 'closed') {
      recovered = true
    }
  } catch {
    /* 恢复也失败：走截断兜底 */
  }
  if (recovered) return
  const s = useChatStore.getState()
  if (s.sessionId === sessionId && s.status === 'streaming') {
    flushPendingTokens()
    s.finalizeStreaming()
    s.clearTodos()
    s.markTurnInterrupted()
    s.setStatus('idle')
    s.setStreamError('连接中断（未收到终止事件），本回合已截断；若后端仍在运行，重进会话可恢复')
  }
}

export function useStreamChat(sessionId: UUID | null, observerEnabled = true): UseStreamChat {
  /** 把单个 SSE 事件分发到 chatStore（共享实现，语音 WS 同源复用） */
  const dispatchEvent = useCallback((e: SSEEvent) => {
    dispatchStreamEvent(e)
  }, [])

  // —— 冷进入水合门（B-Tier1：观察流与 /history 并行、事件按序冲刷）——
  // 观察流请求即刻发起（重叠 RTT），事件先缓冲等历史灌入（settleHydration）
  // 后冲刷：loadHistory 整表替换不冲掉重放中的当前回合，重放也不阻塞灌入
  // 守卫。ref 均只在 effect/回调内读写（render 期零触碰）；历史先于观察流
  // 就绪的时序由 settledRef 标记兜底（建门即 resolve）。
  const gateRef = useRef<{ sid: string; promise: Promise<void>; settle: () => void } | null>(null)
  const settledRef = useRef<Set<string>>(new Set())

  const settleHydration = useCallback(() => {
    if (!sessionId) return
    settledRef.current.add(sessionId)
    if (gateRef.current?.sid === sessionId) gateRef.current.settle()
  }, [sessionId])

  // 卸载不拆观察流：离开页面=关观察窗口而非杀回合（连接随会话域拆除）
  useEffect(() => {
    if (!sessionId || !observerEnabled) return
    if (!gateRef.current || gateRef.current.sid !== sessionId) {
      let settle!: () => void
      const promise = new Promise<void>((resolve) => {
        settle = resolve
      })
      gateRef.current = { sid: sessionId, promise, settle }
      if (settledRef.current.has(sessionId)) settle() // 历史先于观察流就绪
    }
    startObserver(sessionId, true, dispatchEvent, gateRef.current.promise)
  }, [sessionId, observerEnabled, dispatchEvent])

  // 水合门兜底：迟迟未 settle（历史查询失败/挂起）时放行，降级为仅重放内容
  useEffect(() => {
    if (!sessionId || !observerEnabled) return
    const timer = setTimeout(settleHydration, HYDRATION_FALLBACK_MS)
    return () => clearTimeout(timer)
  }, [sessionId, observerEnabled, settleHydration])

  /** 发起新消息流（queued-send §3/§6）：
   *  idle=直发（接管消费通道，既有行为）；streaming/interrupted=排队侧发送——
   *  不接管 livePost/观察流（正在跑的回合照常消费），后端决策树分流：
   *  202 入队 → 回滚乐观消息改挂队列条带；200 SSE（并发收尾竞态）→ 事件照常
   *  分发，终态由状态机收敛。 */
  const send = useCallback(
    async (content: string, opts?: SendOptions) => {
      if (!sessionId) return
      const store = useChatStore.getState()
      // idle=直发；streaming/interrupted=排队；error 由横幅复位后重试
      const prevStatus = store.status
      if (prevStatus !== 'idle' && prevStatus !== 'streaming' && prevStatus !== 'interrupted') {
        return
      }
      const sideSend = prevStatus !== 'idle'

      // 先快照待发附件 + 启用知识库：既要挂到 user 消息上（会话窗内展示 KB 徽标/
      // 附件缩略），又要写进请求体（后端同样把 kb_ids 旁挂进消息 additional_kwargs，
      // 历史/回撤回显走同一形状 → 前后进入会话渲染一致）
      const attachs = store.attachments
      const kbSnap = store.selectedKbs.map((kb) => ({ id: kb.id, name: kb.name }))
      const kbIds = store.selectedKbs.map((kb) => kb.id)
      store.pushUser(content, attachs, kbSnap)
      store.setStreamError(null)
      store.setStatus('streaming')
      store.setStreamSource('sse')

      const body = {
        content,
        // 本回合智能体（§4.4 写者）：selectedAgentId 是纯 UI 态，随请求下发；
        // 后端发送成功后写入 last_agent_id（选中永不落库，审计归因不被 UI 污染）
        agent_id: store.selectedAgentId ?? undefined,
        rubric: opts?.rubric,
        attachments: attachs.map((a) => ({ upload_id: a.upload_id })),
        knowledge_base_ids: kbIds.length ? kbIds : undefined,
      }
      // 已把附件 id + kb id 捕获进 body，立即清空 composer（与文本输入 setValue('') 同步，
      // 不再等到 done 事件，避免"发送后还贴在输入框"的观感）
      store.clearAttachments()
      store.clearKbs()

      let controller: AbortController | null = null
      if (!sideSend) {
        stopObserver(sessionId) // 本地消费即将开始，观察流让位（防双路）
        abortLivePost(sessionId) // 防御：同会话残留旧连接
        controller = new AbortController()
        livePost = { sessionId, controller }
      }
      const aborted = () => controller?.signal.aborted ?? false

      let queuedItem: QueuedItem | null = null
      try {
        const termination = await streamApi.sendMessage(
          sessionId,
          body,
          {
            onEvent: dispatchEvent,
            onQueued: (item) => {
              queuedItem = item as QueuedItem
            },
          },
          controller?.signal,
        )
        if (!sideSend && livePost?.controller === controller) livePost = null
        if (termination === 'queued' && queuedItem) {
          // 入队：乐观消息回滚（队列是候客厅，不走时间线），条目改挂队列条带；
          // 状态回到发送前（streaming=回合仍在跑，interrupted=审批卡回归）
          const s = useChatStore.getState()
          flushPendingTokens()
          s.finalizeStreaming()
          const lastUser = [...s.messages].reverse().find((m) => m.role === 'user')
          if (lastUser) s.removeTurn(lastUser.id)
          s.enqueueLocal(queuedItem)
          s.setStatus(prevStatus)
          if (prevStatus === 'idle') s.setStreamSource(null)
          // 回合仍在跑：sideSend 未接管消费通道，幂等补挂观察流兜底断连。
          // entryGuard：本地 POST/语音仍消费时即刻退出，不双路分发同一 run
          //（双路会把余下 token 重复 append、done 双通道触发并发队列回查）
          if (prevStatus === 'streaming') startObserver(sessionId, true, dispatchEvent, null)
          return
        }
        if (termination === 'closed' && !aborted()) {
          // 后端被硬杀/代理掐断：run 可能仍在服务端执行 —— 观察流接续或截断
          await recoverOrTruncate(sessionId, dispatchEvent)
        }
      } catch (err) {
        if (!sideSend && livePost?.controller === controller) livePost = null
        // 后端拒绝（携带真实 detail 的 4xx：未选择智能体 / 队列已满 / HITL 门禁 /
        // 模型不可用等）：请求未成立、无 run——回滚乐观消息、恢复 composer 内容、
        // 以真实原因落横幅。注意顺序：setStreamError 会连带置 status='error'，
        // 须在其后再显式回发送前状态，横幅保留而发送解锁。
        if (err instanceof SseRejectedError && !aborted()) {
          const s = useChatStore.getState()
          flushPendingTokens()
          s.finalizeStreaming()
          const lastUser = [...s.messages].reverse().find((m) => m.role === 'user')
          if (lastUser) s.removeTurn(lastUser.id)
          s.setAttachments(attachs)
          s.setKbs(kbSnap)
          s.setDraft(content)
          s.setStreamError(err.message)
          s.setStatus(sideSend ? prevStatus : 'idle')
          if (!sideSend) s.setStreamSource(null)
          return
        }
        // 网络层异常：先观察流接续（run 可能仍在），失败再落 error（准则 F4）
        if (!aborted()) {
          await recoverOrTruncate(sessionId, dispatchEvent)
        }
      }
    },
    [sessionId, dispatchEvent],
  )

  /** HITL 决策恢复流 */
  const resume = useCallback(
    async (decisions: Decision[]) => {
      if (!sessionId) return
      const store = useChatStore.getState()
      if (store.status !== 'interrupted') return

      store.clearInterrupt()
      store.setStatus('streaming')
      store.setStreamSource('sse')

      stopObserver(sessionId)
      abortLivePost(sessionId)
      const controller = new AbortController()
      livePost = { sessionId, controller }

      try {
        const termination = await streamApi.resume(
          sessionId,
          { decisions },
          { onEvent: dispatchEvent },
          controller.signal,
        )
        if (livePost?.controller === controller) livePost = null
        if (termination === 'closed' && !controller.signal.aborted) {
          await recoverOrTruncate(sessionId, dispatchEvent)
        }
      } catch (err) {
        if (livePost?.controller === controller) livePost = null
        if (!controller.signal.aborted && err instanceof SseRejectedError) {
          // 后端拒绝（如模型不可用 409）：横幅以真实原因呈现并回 idle；待审批
          // 卡在 resume 开始时已清——经真值查询重建（成功则回到 interrupted，
          // 审批不丢失；204/失败则维持 idle + 横幅）
          const s = useChatStore.getState()
          flushPendingTokens()
          s.finalizeStreaming()
          s.setStreamError(err.message)
          s.setStatus('idle')
          s.setStreamSource(null)
          try {
            const payload = await streamApi.getInterrupt(
              sessionId,
              useChatStore.getState().selectedAgentId,
            )
            if (payload?.action_requests?.length) {
              useChatStore.getState().setInterrupt(payload)
            }
          } catch {
            /* 保持 idle + 横幅 */
          }
          return
        }
        if (!controller.signal.aborted) {
          await recoverOrTruncate(sessionId, dispatchEvent)
        }
      }
    },
    [sessionId, dispatchEvent],
  )

  const cancel = useCallback(() => {
    flushPendingTokens()
    if (sessionId) {
      stopObserver(sessionId)
      abortLivePost(sessionId)
      // 服务端取消（best-effort）：广播 canceled 终态 + 冻结队列（pending 批量
      // canceled，两级语义见后端）。本页订阅已拆（取消的常态），由下方本地
      // 落态兜底；失败（网络）时 run 自然跑完，usage 照常入库，重进可见结果
      void streamApi.cancel(sessionId).catch(() => undefined)
    }
    const store = useChatStore.getState()
    // 队列本地批量标记 canceled（服务端真值随后续回查对齐；失败则回 pending）
    store.markQueueCanceledLocal()
    // 中止后固化仍在流式的消息（转 Markdown 渲染，而非停留在纯文本流式态），再回 idle；
    // 同时拆除本回合的任务清单浮窗——中止后计划已死，悬挂只会误导（旧清单累计的诱因之一）；
    // 时间线末尾落「用户已手动中断」提示条目（agent 输出气泡下方，见 MessageList）
    store.finalizeStreaming()
    store.clearTodos()
    store.markTurnInterrupted()
    store.setStatus('idle')
  }, [sessionId])

  const status = useChatStore((s) => s.status)

  return { status, send, resume, cancel, settleHydration }
}
