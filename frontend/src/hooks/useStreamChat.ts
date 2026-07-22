/* 聊天 SSE 状态机 hook（DESIGN §8 核心）。
   职责：把后端 SSE 事件流映射到 chatStore 动作；维护 idle/streaming/interrupted/error。
   - send：发新消息（仅 idle 可用）
   - resume：HITL 决策恢复（仅 interrupted 可用）
   - cancel：中止当前流
   终态仅由 done/interrupt/error 决定（准则 F3）。 */
import { useCallback, useEffect, useRef } from 'react'
import { streamApi } from '@/api/stream'
import { useChatStore } from '@/store/chatStore'
import type {
  AgentMessagePayload,
  ChatStreamStatus,
  Decision,
  InterruptPayload,
  SSEEvent,
  ToolCallPayload,
  ToolResultPayload,
  UUID,
} from '@/types'

export interface SendOptions {
  rubric?: string
  // —— KB 留口（kb-hybrid-access-plan，后端未上线前不传）——
  kbEnabled?: boolean
  kbCollections?: string[]
  kbVersions?: Record<string, string>
}

export interface UseStreamChat {
  status: ChatStreamStatus
  send: (content: string, opts?: SendOptions) => Promise<void>
  resume: (decisions: Decision[]) => Promise<void>
  cancel: () => void
}

export function useStreamChat(sessionId: UUID | null): UseStreamChat {
  const abortRef = useRef<AbortController | null>(null)

  // 卸载时中止流，防内存泄漏
  useEffect(() => {
    return () => abortRef.current?.abort()
  }, [])

  /** 把单个 SSE 事件分发到 chatStore */
  const dispatchEvent = useCallback((e: SSEEvent) => {
    const store = useChatStore.getState()
    switch (e.type) {
      case 'agent_message': {
        const p = e.payload as unknown as AgentMessagePayload
        store.pushAgent(e.agent_path ?? 'root', p.content)
        break
      }
      case 'tool_call': {
        const p = e.payload as unknown as ToolCallPayload
        store.upsertToolCall({
          id: p.id ?? `${p.name}-${Date.now()}`,
          name: p.name,
          args: p.args ?? {},
          caller: p.caller,
          agentPath: e.agent_path ?? 'root',
        })
        break
      }
      case 'tool_result': {
        const p = e.payload as unknown as ToolResultPayload
        store.setToolResult(p.tool_call_id, p.content)
        break
      }
      case 'interrupt':
        store.setInterrupt(e.payload as unknown as InterruptPayload)
        break
      case 'done':
        store.clearAttachments()
        store.setStatus('idle')
        break
      case 'error': {
        const p = e.payload as { message?: string }
        store.setStreamError(p.message ?? '会话出错')
        break
      }
      default:
        // token 等暂未启用事件，忽略
        break
    }
  }, [])

  /** 发起新消息流 */
  const send = useCallback(
    async (content: string, opts?: SendOptions) => {
      if (!sessionId) return
      const store = useChatStore.getState()
      // 仅 idle 可发（interrupted 走 resume）
      if (store.status !== 'idle') return

      store.pushUser(content)
      store.setStreamError(null)
      store.setStatus('streaming')

      const attachs = store.attachments
      const body = {
        content,
        rubric: opts?.rubric,
        attachments: attachs.map((a) => ({ upload_id: a.upload_id })),
        kb_enabled: opts?.kbEnabled,
        kb_collections: opts?.kbCollections,
        kb_versions: opts?.kbVersions,
      }

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        await streamApi.sendMessage(sessionId, body, { onEvent: dispatchEvent }, controller.signal)
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

      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller

      try {
        await streamApi.resume(
          sessionId,
          { decisions },
          { onEvent: dispatchEvent },
          controller.signal,
        )
      } catch {
        if (!controller.signal.aborted) {
          useChatStore.getState().setStreamError('网络异常，恢复已中断')
        }
      }
    },
    [sessionId, dispatchEvent],
  )

  const cancel = useCallback(() => {
    abortRef.current?.abort()
    useChatStore.getState().setStatus('idle')
  }, [])

  const status = useChatStore((s) => s.status)

  return { status, send, resume, cancel }
}
