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
import { sessionsApi } from '@/api/sessions'
import { streamApi } from '@/api/stream'
import { useChatStore } from '@/store/chatStore'
import { dispatchStreamEvent, flushPendingTokens } from '@/lib/streamEvents'
import { SseRejectedError, type StreamTermination } from '@/lib/sse'
import type { ChatStreamStatus, Decision, SSEEvent, UUID } from '@/types'

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
  }
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

  /** 发起新消息流 */
  const send = useCallback(
    async (content: string, opts?: SendOptions) => {
      if (!sessionId) return
      const store = useChatStore.getState()
      // 仅 idle 可发（interrupted 走 resume）
      if (store.status !== 'idle') return

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

      stopObserver(sessionId) // 本地消费即将开始，观察流让位（防双路）
      abortLivePost(sessionId) // 防御：同会话残留旧连接
      const controller = new AbortController()
      livePost = { sessionId, controller }

      try {
        const termination = await streamApi.sendMessage(sessionId, body, { onEvent: dispatchEvent }, controller.signal)
        if (livePost?.controller === controller) livePost = null
        if (termination === 'closed' && !controller.signal.aborted) {
          // 后端被硬杀/代理掐断：run 可能仍在服务端执行 —— 观察流接续或截断
          await recoverOrTruncate(sessionId, dispatchEvent)
        }
      } catch (err) {
        if (livePost?.controller === controller) livePost = null
        // 后端拒绝（携带真实 detail 的 4xx：未选择智能体 / HITL 门禁 / 模型
        // 不可用等）：请求未成立、无 run——回滚乐观消息、恢复 composer 内容、
        // 以真实原因落横幅。注意顺序：setStreamError 会连带置 status='error'，
        // 须在其后再显式回 idle，横幅保留而发送解锁。
        if (err instanceof SseRejectedError && !controller.signal.aborted) {
          const s = useChatStore.getState()
          flushPendingTokens()
          s.finalizeStreaming()
          const lastUser = [...s.messages].reverse().find((m) => m.role === 'user')
          if (lastUser) s.removeTurn(lastUser.id)
          s.setAttachments(attachs)
          s.setKbs(kbSnap)
          s.setDraft(content)
          s.setStreamError(err.message)
          s.setStatus('idle')
          s.setStreamSource(null)
          return
        }
        // 网络层异常：先观察流接续（run 可能仍在），失败再落 error（准则 F4）
        if (!controller.signal.aborted) {
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
      // 服务端取消（best-effort）：广播 canceled 终态。本页订阅已拆（取消的
      // 常态），由下方本地落态兜底，语义与终态事件一致；失败（网络）时 run
      // 自然跑完，usage 照常入库，重进可见结果
      void streamApi.cancel(sessionId).catch(() => undefined)
    }
    const store = useChatStore.getState()
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
