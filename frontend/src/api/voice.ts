/* 语音输入 WS 连接层 —— 对应后端 app/api/voice.py（WS /sessions/{id}/voice）。
   职责（连接形态核心）：
   - 预建连：sessionId 就绪即创建 WS（与按键解耦），按压零握手延迟；
   - 常驻复用：多回合共用同一连接；心跳 ping 自动回 pong；
   - 断线重连：指数退避（1s→15s cap），重连期间 ready=false（麦克风置灰）；
   - watchdog：ready 后 60s 无任何下行帧 → 主动 close 触发重连；
   - 回合中断恢复：onTurnAborted 回调（推理中掉线按网络异常同款恢复）。
   按压/松开/中止只发帧，不管连接：press=start 帧、release=flush 帧、
   cancelStream=cancel 帧（推理期中止语音发起的 astream，本地即视为回合结束）。 */
import type { SSEEvent, UUID, VoiceDownFrame } from '@/types'

export interface VoiceConnectionHandlers {
  /** 连接就绪/失去（麦克风按钮可按压性） */
  onReadyChange: (ready: boolean) => void
  /** 下行业务帧（stt_* 与 agent 事件；ping 在内部消化） */
  onFrame: (frame: VoiceDownFrame) => void
  /** 回合中断线（recording/transcribing/streaming 中掉线）→ 按网络异常恢复 */
  onTurnAborted: (reason: string) => void
}

const RECONNECT_BASE_MS = 1_000
const RECONNECT_MAX_MS = 15_000
const WATCHDOG_MS = 60_000

function backoff(attempt: number): number {
  return Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt)
}

/** 由当前页面地址 + API base 推导语音 WS URL（兼容相对 /api 与绝对 http(s) base）。 */
function voiceWsUrl(sessionId: UUID): string {
  const base = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '')
  if (/^https?:\/\//i.test(base)) {
    // 绝对地址（生产直连）：http(s) → ws(s)
    return `${base.replace(/^http/i, 'ws')}/sessions/${sessionId}/voice`
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}${base}/sessions/${sessionId}/voice`
}

/** 单会话的语音 WS 管理器（useVoiceInput 持有；sessionId 变化即销毁重建） */
export class VoiceConnection {
  private ws: WebSocket | null = null
  private sessionId: UUID
  private handlers: VoiceConnectionHandlers
  private alive = false
  private retry = 0
  private watchdogTimer: ReturnType<typeof setInterval> | null = null
  private lastFrameAt = 0
  /** 本回合是否已发 start 且未收终态（done/error/stt_empty/中断） */
  private turnOpen = false

  constructor(sessionId: UUID, handlers: VoiceConnectionHandlers) {
    this.sessionId = sessionId
    this.handlers = handlers
  }

  /** 建立连接（幂等；供挂载时调用） */
  start(): void {
    this.alive = true
    this.connect()
  }

  /** 销毁：关连接、停定时器；回合若在进行则视为中断 */
  stop(): void {
    this.alive = false
    this.clearWatchdog()
    if (this.turnOpen) {
      this.turnOpen = false
      this.handlers.onTurnAborted('会话页已离开')
    }
    this.ws?.close()
    this.ws = null
  }

  get ready(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  private connect(): void {
    if (!this.alive) return
    const ws = new WebSocket(voiceWsUrl(this.sessionId))
    ws.binaryType = 'arraybuffer'
    this.ws = ws

    ws.onopen = () => {
      if (!this.alive) return
      this.retry = 0
      this.lastFrameAt = Date.now()
      this.startWatchdog()
      this.handlers.onReadyChange(true)
    }

    ws.onmessage = (e) => {
      this.lastFrameAt = Date.now()
      if (typeof e.data !== 'string') return // 音频方向仅上行，防御性忽略
      let frame: VoiceDownFrame
      try {
        frame = JSON.parse(e.data) as VoiceDownFrame
      } catch {
        return
      }
      if (!frame || typeof frame.type !== 'string') return
      if (frame.type === 'ping') {
        this.send({ type: 'pong' })
        return
      }
      // 回合终态：agent done/error/canceled（interrupt 除外——HITL 仍待决）与 stt_empty
      if (
        this.turnOpen &&
        (frame.type === 'done' || frame.type === 'error' || frame.type === 'canceled' ||
          frame.type === 'stt_empty')
      ) {
        this.turnOpen = false
      }
      this.handlers.onFrame(frame)
    }

    ws.onclose = (e) => {
      this.clearWatchdog()
      this.handlers.onReadyChange(false)
      if (!this.alive) return
      // 4404/4409 连接级永久错误（会话不存在/归档、智能体不可构建）：
      // 重连必得同样结果，停摆（麦克风保持置灰；重新进页会重建）
      if (e.code === 4404 || e.code === 4409) return
      if (this.turnOpen) {
        this.turnOpen = false
        this.handlers.onTurnAborted('语音连接已断开')
      }
      setTimeout(() => this.connect(), backoff(this.retry++))
    }

    ws.onerror = () => {
      // onclose 随后触发，统一在 onclose 处理重连
    }
  }

  // ---------------------------------------------------------------- 回合帧
  /** 按下麦克风：start 快照帧（标记回合开启，音频随后上行）。
   *  agent_id=本回合智能体（会话-智能体解耦写者语义，缺省回落 last_agent_id） */
  pressStart(payload: {
    draft?: string | null
    agent_id?: string | null
    attachments: { upload_id: string }[]
    knowledge_base_ids: string[]
    rubric?: string | null
  }): boolean {
    const ok = this.send({
      type: 'start',
      draft: payload.draft ?? null,
      agent_id: payload.agent_id ?? null,
      attachments: payload.attachments,
      knowledge_base_ids: payload.knowledge_base_ids,
      rubric: payload.rubric ?? null,
    })
    if (ok) this.turnOpen = true
    return ok
  }

  /** 按住期间：PCM16 音频帧（二进制直发） */
  sendAudio(pcm: ArrayBuffer): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(pcm)
    }
  }

  /** 松开麦克风：flush（即使中途出错也发，保证后端回合正常收口） */
  releaseFlush(): void {
    this.send({ type: 'flush' })
  }

  /** 中止语音发起的推理回合：发 cancel 帧（后端取消 run 并广播 canceled 终态，
      释放 withdraw 闸）。终态帧随后经隧道到达（与 SSE 路径同契约），本地
      落态由 dispatch 统一处理——turnOpen 即刻关闭仅表示"不再有上行"。 */
  cancelStream(): void {
    if (!this.turnOpen) return
    if (this.send({ type: 'cancel' })) {
      this.turnOpen = false
    }
  }

  // ---------------------------------------------------------------- 内部
  private send(frame: unknown): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false
    this.ws.send(JSON.stringify(frame))
    return true
  }

  private startWatchdog(): void {
    this.clearWatchdog()
    this.watchdogTimer = setInterval(() => {
      if (Date.now() - this.lastFrameAt > WATCHDOG_MS) {
        // 静默连接（服务端 ping 丢失等）→ 主动断开走重连路径
        this.ws?.close()
      }
    }, 5_000)
  }

  private clearWatchdog(): void {
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer)
      this.watchdogTimer = null
    }
  }
}

/** 类型断言辅助：语音下行帧中属于 agent 事件（复用 SSE dispatch）的部分 */
export function asSSEEvent(frame: VoiceDownFrame): SSEEvent | null {
  const agentTypes: readonly string[] = [
    'token',
    'tool_call',
    'tool_result',
    'thinking',
    'agent_message',
    'todos',
    'file_change',
    'usage',
    'status',
    'interrupt',
    'done',
    'error',
    'canceled',
  ]
  if (!agentTypes.includes(frame.type)) return null
  return frame as unknown as SSEEvent
}
