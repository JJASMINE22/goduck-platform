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
  FileChangePayload,
  InterruptPayload,
  SSEEvent,
  ThinkingPayload,
  TodoItem,
  TokenPayload,
  ToolCallPayload,
  ToolResultPayload,
  UUID,
} from '@/types'

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
  useEffect(() => {
    return () => {
      const controller = abortRef.current
      controller?.abort()
      const store = useChatStore.getState()
      if (controller && store.status === 'streaming') {
        store.finalizeStreaming()
        store.setStatus('idle')
      }
    }
  }, [])

  /** 把单个 SSE 事件分发到 chatStore */
  const dispatchEvent = useCallback((e: SSEEvent) => {
    const store = useChatStore.getState()
    switch (e.type) {
      case 'token': {
        // 逐 token 增量：聚合进流式消息（content 或 reasoning）
        const p = e.payload as unknown as TokenPayload
        store.appendToken(e.agent_path ?? 'root', p.id, p.delta, !!p.reasoning)
        break
      }
      case 'agent_message': {
        const p = e.payload as unknown as AgentMessagePayload
        store.pushAgent(e.agent_path ?? 'root', p.content)
        break
      }
      case 'thinking': {
        const p = e.payload as unknown as ThinkingPayload
        store.pushReasoning(e.agent_path ?? 'root', p.content)
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
      case 'todos': {
        // 任务规划清单：整表替换该代理的 todos（后端按 agent_path 变更去重推送）
        const p = e.payload as unknown as { todos: TodoItem[] }
        store.setTodos(e.agent_path ?? 'root', p.todos ?? [])
        break
      }
      case 'file_change': {
        // 工作区文件变更徽标：一批落盘记录追加进本回合时间线
        const p = e.payload as unknown as FileChangePayload
        store.applyFileChange(Array.isArray(p.records) ? p.records : [])
        break
      }
      case 'interrupt':
        // 流式在 HITL 处暂停：固化已到达的部分文本再进入待审批态
        store.finalizeStreaming()
        store.setInterrupt(e.payload as unknown as InterruptPayload)
        break
      case 'done':
        store.finalizeStreaming()
        store.clearAttachments()
        store.setStatus('idle')
        break
      case 'error': {
        const p = e.payload as { message?: string }
        store.finalizeStreaming()
        store.setStreamError(p.message ?? '会话出错')
        break
      }
      default:
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

      // 先快照待发附件 + 启用知识库：既要挂到 user 消息上（会话窗内展示），又要写进请求体
      const attachs = store.attachments
      const kbIds = store.selectedKbs.map((kb) => kb.id)
      store.pushUser(content, attachs)
      store.setStreamError(null)
      store.setStatus('streaming')

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
    const store = useChatStore.getState()
    // 中止后固化仍在流式的消息（转 Markdown 渲染，而非停留在纯文本流式态），再回 idle
    store.finalizeStreaming()
    store.setStatus('idle')
  }, [])

  const status = useChatStore((s) => s.status)

  return { status, send, resume, cancel }
}
