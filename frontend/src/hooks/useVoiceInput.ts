/* 语音输入交互 hook —— 长按麦克风 → PCM 采集分帧上行 → 松开 flush → 终稿自动成消息。
   时序联动（plan §3.2）：
   - press：快照 chatStore 附件/KB/rubric → start 帧 → 采麦；voiceState=recording；
   - stt_chunk：interimTranscript 实时刷新（composer 流式渲染中间稿）；
   - release：停采麦 → flush 帧；voiceState=transcribing（即使中途出错也发 flush，
     保证后端回合正常收口；后端对"错误后新 start"另有超越语义兜底）；
   - stt_output：pushUser(终稿+附件快照) + clearAttachments/clearKbs + status=streaming
     （镜像手打 send() 的时点，终稿到推理启动的间隙内输入也已锁定）；
   - agent 事件经共享 dispatchStreamEvent 写入（渲染与 SSE 完全一致）；
   - stt_empty：复位麦克风（无消息无 astream）；error：现有 error 落态 + 复位。
   采麦：AudioWorklet 直出 Float32 → 主线程降采样 16kHz → Int16 PCM，100ms/帧上行。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { asSSEEvent, VoiceConnection } from '@/api/voice'
import { dispatchStreamEvent } from '@/lib/streamEvents'
import { useChatStore } from '@/store/chatStore'
import { toastError, toastInfo } from '@/lib/antdStatic'
import type { SttTranscriptPayload, UUID, VoiceDownFrame } from '@/types'

const TARGET_SAMPLE_RATE = 16_000
const FRAME_MS = 100

/** AudioWorklet 内联脚本：直通原始 Float32 帧（重采样在主线程做，逻辑集中一处） */
const WORKLET_SRC = `
class PassthroughProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0]?.[0]
    if (ch) this.port.postMessage(ch.slice(0))
    return true
  }
}
registerProcessor('voice-passthrough', PassthroughProcessor)
`

/** 线性降采样 Float32 → Int16 PCM（输入采样率 → 16kHz）。 */
function downsampleToPcm16(input: Float32Array, inputRate: number): ArrayBuffer {
  if (inputRate === TARGET_SAMPLE_RATE) {
    const out = new Int16Array(input.length)
    for (let i = 0; i < input.length; i++) out[i] = floatToInt16(input[i])
    return out.buffer
  }
  const ratio = inputRate / TARGET_SAMPLE_RATE
  const outLen = Math.floor(input.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    // 线性插值取目标位置样本
    const pos = i * ratio
    const idx = Math.floor(pos)
    const frac = pos - idx
    const s = input[idx] + (input[idx + 1] - input[idx]) * frac
    out[i] = floatToInt16(s)
  }
  return out.buffer
}

function floatToInt16(v: number): number {
  const c = Math.max(-1, Math.min(1, v))
  return c < 0 ? c * 0x8000 : c * 0x7fff
}

export interface UseVoiceInput {
  /** WS 连接是否就绪（预建连成功；麦克风按钮仅 ready && idle 可按） */
  ready: boolean
  /** 按下麦克风（pointerdown）；rubric 为 composer 当前单次覆盖值快照 */
  press: (rubric?: string) => Promise<void>
  /** 松开麦克风（pointerup/pointercancel/pointerleave）——无论状态一律拆采麦 */
  release: () => void
}

export function useVoiceInput(sessionId: UUID | null): UseVoiceInput {
  const [ready, setReady] = useState(false)
  const connRef = useRef<VoiceConnection | null>(null)

  // —— 采麦运行时（press 时建立，release 时整体拆除）——
  const streamRef = useRef<MediaStream | null>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const nodeRef = useRef<AudioWorkletNode | null>(null)
  /** 采麦是否物理运行中（与 voiceState 独立：error 复位后麦克风可能仍被按住） */
  const capturingRef = useRef(false)
  /** 累积缓冲：float 帧到达即攒，满 FRAME_MS 量即整帧 PCM 上行 */
  const bufRef = useRef<Float32Array[]>([])
  const bufLenRef = useRef(0)
  const frameSamplesRef = useRef(0)
  const releasingRef = useRef(false)

  // —— 采麦拆除（幂等；断开节点/停轨道/关上下文）——
  const teardownCapture = useCallback(() => {
    capturingRef.current = false
    releasingRef.current = true
    nodeRef.current?.port.close()
    nodeRef.current?.disconnect()
    nodeRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    ctxRef.current?.close().catch(() => undefined)
    ctxRef.current = null
    bufRef.current = []
    bufLenRef.current = 0
  }, [])

  // —— 下行帧路由：stt_* 驱动语音状态机，agent 事件走共享 dispatch ——
  const handleFrame = useCallback((frame: VoiceDownFrame) => {
    const store = useChatStore.getState()
    switch (frame.type) {
      case 'stt_chunk': {
        const p = frame.payload as unknown as SttTranscriptPayload
        store.setInterimTranscript(p.transcript ?? '')
        break
      }
      case 'stt_output': {
        // 合并终稿（后端单帧下发）：固化用户消息并进入 streaming（终稿→推理
        // 间隙也锁输入）；多句长按已在后端合并为一条消息
        const p = frame.payload as unknown as SttTranscriptPayload
        const finalText = (p.transcript ?? '').trim()
        store.setVoiceState('idle')
        store.setInterimTranscript('')
        if (finalText && store.status === 'idle') {
          const attachs = store.attachments
          store.pushUser(finalText, attachs)
          store.setStreamError(null)
          store.clearAttachments()
          store.clearKbs()
          store.setStatus('streaming')
        }
        break
      }
      case 'stt_empty': {
        const wasTranscribing = store.voiceState === 'transcribing'
        store.setInterimTranscript('')
        if (store.voiceState !== 'idle') store.setVoiceState('idle')
        // 采麦失败路径（press catch 已复位 + 报错）不再重复提示
        if (wasTranscribing) toastInfo('未识别到语音')
        break
      }
      default: {
        // agent 事件（token/tool_call/.../done/interrupt/error）：与 SSE 同源分发
        const sse = asSSEEvent(frame)
        if (!sse) return
        dispatchStreamEvent(sse)
        // 终态/中断后麦克风视作复位（interrupt 时 HITL 卡接管输入；
        // error 复位后再按 start 由后端"超越"语义开新回合，或 release 收口旧回合）
        if (sse.type === 'done' || sse.type === 'error' || sse.type === 'interrupt') {
          const cur = useChatStore.getState()
          if (cur.voiceState !== 'idle') cur.setVoiceState('idle')
        }
        break
      }
    }
  }, [])

  /** 回合中断线（录音/转写/推理中掉线）→ useStreamChat 网络异常同款恢复 */
  const handleTurnAborted = useCallback(
    (reason: string) => {
      teardownCapture() // 麦克风轨道必须释放（浏览器指示灯/权限条）
      const store = useChatStore.getState()
      // 录音/转写阶段掉线：无消息无推理，仅复位麦克风
      if (store.voiceState !== 'idle') {
        store.setVoiceState('idle')
        store.setInterimTranscript('')
        toastError(`${reason}，语音输入已取消`)
        return
      }
      // 推理中掉线：按网络异常恢复（固化半截 + error 落态，准则 F4）
      if (store.status === 'streaming') {
        store.finalizeStreaming()
        store.setStreamError(`${reason}，回复已中断`)
      }
    },
    [teardownCapture],
  )

  // —— 预建连：sessionId 就绪即创建，与按键完全解耦；卸载/切换会话即销毁 ——
  useEffect(() => {
    if (!sessionId) return
    const conn = new VoiceConnection(sessionId, {
      onReadyChange: setReady,
      onFrame: handleFrame,
      onTurnAborted: handleTurnAborted,
    })
    connRef.current = conn
    conn.start()
    return () => {
      conn.stop()
      connRef.current = null
      setReady(false)
    }
  }, [sessionId, handleFrame, handleTurnAborted])

  useEffect(() => teardownCapture, [teardownCapture])

  // —— 按下：快照 → start 帧 → 采麦 ——
  const press = useCallback(
    async (rubric?: string) => {
      const store = useChatStore.getState()
      const conn = connRef.current
      if (!conn || !conn.ready) return
      if (store.voiceState !== 'idle' || store.status !== 'idle') return

      // 附件/KB/rubric 快照（选完再长按即自然生效；校验错误在录音前由 error 帧暴露）
      const attachs = store.attachments
      const kbIds = store.selectedKbs.map((kb) => kb.id)
      const ok = conn.pressStart({
        attachments: attachs.map((a) => ({ upload_id: a.upload_id })),
        knowledge_base_ids: kbIds,
        rubric: rubric?.trim() || undefined,
      })
      if (!ok) return

      store.setVoiceState('recording')
      store.setInterimTranscript('')
      releasingRef.current = false

      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        })
        streamRef.current = stream
        const ctx = new AudioContext()
        ctxRef.current = ctx
        capturingRef.current = true
        frameSamplesRef.current = Math.floor((ctx.sampleRate * FRAME_MS) / 1000)
        const blobUrl = URL.createObjectURL(
          new Blob([WORKLET_SRC], { type: 'application/javascript' }),
        )
        try {
          await ctx.audioWorklet.addModule(blobUrl)
        } finally {
          URL.revokeObjectURL(blobUrl)
        }
        const src = ctx.createMediaStreamSource(stream)
        const node = new AudioWorkletNode(ctx, 'voice-passthrough', {
          numberOfInputs: 1,
          numberOfOutputs: 0,
        })
        nodeRef.current = node
        node.port.onmessage = (e: MessageEvent<Float32Array>) => {
          const conn2 = connRef.current
          if (!conn2 || releasingRef.current) return
          // 攒帧：满 FRAME_MS 量的 float 样本 → 降采样 PCM16 → 二进制上行。
          // 降采样在主线程做（见 downsampleToPcm16），worklet 保持极简直通。
          bufRef.current.push(e.data)
          bufLenRef.current += e.data.length
          if (bufLenRef.current >= frameSamplesRef.current) {
            const merged = new Float32Array(bufLenRef.current)
            let off = 0
            for (const chunk of bufRef.current) {
              merged.set(chunk, off)
              off += chunk.length
            }
            bufRef.current = []
            bufLenRef.current = 0
            const pcm = downsampleToPcm16(merged, ctx.sampleRate)
            conn2.sendAudio(pcm)
          }
        }
        src.connect(node)
      } catch (e) {
        // 采麦失败（拒绝授权/无设备）：发 flush 让后端回合以空转写收场
        teardownCapture()
        conn.releaseFlush()
        store.setVoiceState('idle')
        toastError(e instanceof Error ? `无法访问麦克风：${e.message}` : '无法访问麦克风')
      }
    },
    [teardownCapture],
  )

  // —— 松开：无条件拆采麦；正常路径发 flush 并进入 transcribing ——
  const release = useCallback(() => {
    const conn = connRef.current
    const wasCapturing = capturingRef.current
    teardownCapture()
    if (!conn) return
    const store = useChatStore.getState()
    if (store.voiceState === 'recording') {
      store.setVoiceState('transcribing')
      conn.releaseFlush()
    } else if (wasCapturing) {
      // error 已复位 voiceState 但麦克风仍被按住：仅发 flush 收口后端旧回合
      conn.releaseFlush()
    }
  }, [teardownCapture])

  return { ready, press, release }
}
