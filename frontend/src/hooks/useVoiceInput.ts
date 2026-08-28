/* 语音输入交互 hook —— 长按麦克风 → PCM 采集分帧上行 → 松开 flush → 终稿自动成消息。
    时序联动（plan §3.2）：
    - press：快照 chatStore 草稿/附件/KB/rubric → start 帧 → 采麦；voiceState=recording；
    - stt_chunk：interimTranscript 实时刷新（composer 流式渲染"草稿+中间稿"合并预览）；
    - release：停采麦 → flush 帧；voiceState=transcribing（即使中途出错也发 flush，
      保证后端回合正常收口；后端对"错误后新 start"另有超越语义兜底）；
    - stt_output：pushUser(草稿+终稿合并稿+附件快照) + clearAttachments/clearKbs +
      清空草稿 + status=streaming（镜像手打 send() 的时点，终稿到推理启动的间隙内
      输入也已锁定；合并稿由后端下发，气泡所见即发往 agent 的内容）；
    - agent 事件经共享 dispatchStreamEvent 写入（渲染与 SSE 完全一致）；
    - stt_empty：复位麦克风（无消息无 astream，草稿保留待继续编辑）；error：现有
      error 落态 + 复位（草稿同样保留）。
    - 推理期中止（cancelStream）：cancel 帧 → 后端取消 astream 并释放 withdraw 闸，
      本地落态由调用方完成（与手打 SSE abort 同语义，无终态帧）。
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

/** 线性重采样 Float32 → Int16 PCM（任意输入采样率 ⇄ 16kHz，双向插值）。

降采样：48kHz 话筒/系统混音常见；升采样：蓝牙 HFP 麦克风流可能仅 8/16kHz
（浏览器上下文跟随输入设备时）——单向实现会在升采样时越界读出 NaN→0，变成
纯静音流，供应商侧表现为"无语音"。 */
function downsampleToPcm16(input: Float32Array, inputRate: number): ArrayBuffer {
  if (inputRate === TARGET_SAMPLE_RATE) {
    const out = new Int16Array(input.length)
    for (let i = 0; i < input.length; i++) out[i] = floatToInt16(input[i])
    return out.buffer
  }
  const step = inputRate / TARGET_SAMPLE_RATE
  const outLen = Math.floor((input.length - 1) / step) + 1
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    // 线性插值取目标位置样本（step<1 时为升采样，idx 恒在界内）
    const pos = i * step
    const idx = Math.floor(pos)
    const frac = pos - idx
    const s =
      idx + 1 < input.length ? input[idx] + (input[idx + 1] - input[idx]) * frac : input[idx]
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
  /** 中止语音发起的推理（推理期发 WS cancel 帧；不在回合中则忽略） */
  cancelStream: () => void
}

export function useVoiceInput(sessionId: UUID | null, enabled: boolean): UseVoiceInput {
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
        // 合并稿（后端单帧下发，草稿+终稿）：固化用户消息并进入 streaming（终稿→
        // 推理间隙也锁输入）；多句长按已在后端合并为一条消息，草稿随发送清空
        const p = frame.payload as unknown as SttTranscriptPayload
        const finalText = (p.transcript ?? '').trim()
        store.setVoiceState('idle')
        store.setInterimTranscript('')
        if (finalText && store.status === 'idle') {
          // 快照与手打 send() 同源：附件/KB 挂上用户消息（徽标渲染一致）
          const attachs = store.attachments
          const kbSnap = store.selectedKbs.map((kb) => ({ id: kb.id, name: kb.name }))
          store.pushUser(finalText, attachs, kbSnap)
          store.setStreamError(null)
          store.clearAttachments()
          store.clearKbs()
          store.setDraft('')
          store.setStatus('streaming')
          store.setStreamSource('voice')
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

  // —— 预建连：sessionId 就绪且语音能力开启才创建（禁用时不发无效连接请求）；
  //    与按键完全解耦；卸载/切换会话/能力关闭即销毁 ——
  useEffect(() => {
    if (!sessionId || !enabled) return
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
  }, [sessionId, enabled, handleFrame, handleTurnAborted])

  useEffect(() => teardownCapture, [teardownCapture])

  // —— 按下：快照 → start 帧 → 采麦 ——
  const press = useCallback(
    async (rubric?: string) => {
      const store = useChatStore.getState()
      const conn = connRef.current
      if (!conn || !conn.ready) return
      if (store.voiceState !== 'idle' || store.status !== 'idle') return

      // 草稿/附件/KB/rubric 快照（输完/选完再长按即自然生效；校验错误在录音前由
      // error 帧暴露；录音期间输入框只读，快照即终值，不会被中途修改）
      const attachs = store.attachments
      const kbIds = store.selectedKbs.map((kb) => kb.id)
      const ok = conn.pressStart({
        draft: store.draft,
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
        if (releasingRef.current) {
          // 等待授权期间用户已松开（首用弹权限窗时常见）：立即释放，勿泄漏轨道
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        streamRef.current = stream
        const ctx = new AudioContext()
        // 保险：非严格手势上下文创建的 ctx 可能起点 suspended，显式唤醒
        if (ctx.state === 'suspended') await ctx.resume()
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

  // —— 中止语音发起的推理：WS cancel 帧（后端取消 astream、释放 withdraw 闸）——
  const cancelStream = useCallback(() => {
    connRef.current?.cancelStream()
  }, [])

  return { ready, press, release, cancelStream }
}
