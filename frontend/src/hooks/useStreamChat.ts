/* 聊天 SSE 状态机 hook（DESIGN §8 核心）。
   职责：把后端 SSE 事件流映射到 chatStore 动作；维护 idle/streaming/interrupted/error。
   - send：发新消息（仅 idle 可用）
   - resume：HITL 决策恢复（仅 interrupted 可用）
   - cancel：中止当前流
   终态仅由 done/interrupt/error 决定（准则 F3）。
   注：SSE 事件分发已提取为 lib/streamEvents.dispatchStreamEvent，
   与语音 WS 下行隧道共用同一映射。 */
import { useCallback, useEffect, useRef } from 'react'
import { streamApi } from '@/api/stream'
import { useChatStore } from '@/store/chatStore'
import { dispatchStreamEvent, flushPendingTokens } from '@/lib/streamEvents'
import type { ChatStreamStatus, Decision, UUID } from '@/types'

export interface SendOptions {
  rubric?: string
}

export interface UseStreamChat {
  status: ChatStreamStatus
  send: (content: string, opts?: SendOptions) => Promise<void>
  resume: (decisions: Decision[]) => Promise<void>
  cancel: () => void
}

export function useStreamChat(sessionId: UUID | null): UseStreamChat {
  const abortRef = useRef<AbortController | null>(null)

  // 卸载时中止流，防内存泄漏；若卸载时仍在流式（切页离开），还需固化半截消息
  // 并回 idle——chatStore 在同会话往返时被保留（ChatPage 不再 reset），状态机
  // 卡在"生成中"会让重进后输入框永久锁定（准则 F3：终态必须落定）。
  // 中止语义同 cancel：拆除在途任务清单浮窗，避免重进会话时悬挂旧计划。
  useEffect(() => {
    return () => {
      // 先冲刷 token 合并缓冲再固化：半截消息必须包含已到达未落地的增量
      flushPendingTokens()
      const controller = abortRef.current
      controller?.abort()
      const store = useChatStore.getState()
      if (controller && store.status === 'streaming') {
        store.finalizeStreaming()
        store.clearTodos()
        // 离开页面 = 本回合就地截断：在途工具卡标记已中断（结果永不再到达，
        // 「执行中」不应永挂），同 cancel 语义；半截消息由 finalize 固化
        store.markTurnInterrupted()
        store.setStatus('idle')
      }
    }
  }, [])

  /** 把单个 SSE 事件分发到 chatStore（共享实现，语音 WS 同源复用） */
  const dispatchEvent = useCallback((e: Parameters<typeof dispatchStreamEvent>[0]) => {
    dispatchStreamEvent(e)
  }, [])

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
        rubric: opts?.rubric,
        attachments: attachs.map((a) => ({ upload_id: a.upload_id })),
        knowledge_base_ids: kbIds.length ? kbIds : undefined,
      }
      // 已把附件 id + kb id 捕获进 body，立即清空 composer（与文本输入 setValue('') 同步，
      // 不再等到 done 事件，避免"发送后还贴在输入框"的观感）
      store.clearAttachments()
      store.clearKbs()

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const termination = await streamApi.sendMessage(sessionId, body, { onEvent: dispatchEvent }, controller.signal)
        // 流关闭但未收到任何终止事件（done/interrupt/error）：后端被硬杀/代理掐断。
        // 绝不允许 status 永久停留在 streaming（输入锁死）——落回 idle 并提示；
        // 若后端实际已停在 HITL（事件丢失恰恰发生在 interrupt 落库后），会话重入
        // 恢复（ChatPage /interrupt 查询）仍可重建审批卡。
        if (termination === 'closed' && !controller.signal.aborted) {
          const store2 = useChatStore.getState()
          if (store2.status === 'streaming') {
            store2.finalizeStreaming()
            store2.clearTodos()
            store2.markTurnInterrupted()
            store2.setStatus('idle')
            store2.setStreamError('连接中断（未收到终止事件），本回合已截断；若后端停在待审批，重进会话可恢复')
          }
        }
      } catch {
        // 网络层异常：error floor，兜底转 error 状态（准则 F4）
        if (!controller.signal.aborted) {
          useChatStore.getState().setStreamError('网络异常，流已中断')
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

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        const termination = await streamApi.resume(
          sessionId,
          { decisions },
          { onEvent: dispatchEvent },
          controller.signal,
        )
        if (termination === 'closed' && !controller.signal.aborted) {
          const store2 = useChatStore.getState()
          if (store2.status === 'streaming') {
            store2.finalizeStreaming()
            store2.clearTodos()
            store2.markTurnInterrupted()
            store2.setStatus('idle')
            store2.setStreamError('恢复流被中断（未收到终止事件）；若后端仍停在待审批，重进会话可恢复')
          }
        }
      } catch {
        if (!controller.signal.aborted) {
          useChatStore.getState().setStreamError('网络异常，恢复已中断')
        }
      }
    },
    [sessionId, dispatchEvent],
  )

  const cancel = useCallback(() => {
    // 中止前冲刷 token 合并缓冲：固化半截消息需含缓冲中的尾部增量
    flushPendingTokens()
    abortRef.current?.abort()
    const store = useChatStore.getState()
    // 中止后固化仍在流式的消息（转 Markdown 渲染，而非停留在纯文本流式态），再回 idle；
    // 同时拆除本回合的任务清单浮窗——中止后计划已死，悬挂只会误导（旧清单累计的诱因之一）；
    // 时间线末尾落「用户已手动中断」提示条目（agent 输出气泡下方，见 MessageList）
    store.finalizeStreaming()
    store.clearTodos()
    store.markTurnInterrupted()
    store.setStatus('idle')
  }, [])

  const status = useChatStore((s) => s.status)

  return { status, send, resume, cancel }
}
