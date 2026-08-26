/* 流式事件 → chatStore 动作的共享分发器。
   两个来源共用同一映射（准则：单一消费路径，视图行为一致）：
   - SSE（useStreamChat：POST /messages、/resume）；
   - 语音 WS 下行隧道（useVoiceInput：stt_output 触发 astream 后的 agent 事件）。
   注意：done/error 的"终态收尾"（clearAttachments 等）在此统一处理，
   与 useStreamChat 原实现行为逐帧一致。 */
import { useChatStore } from '@/store/chatStore'
import type {
  AgentMessagePayload,
  FileChangePayload,
  InterruptPayload,
  SSEEvent,
  StatusPayload,
  ThinkingPayload,
  TodoItem,
  TokenPayload,
  ToolCallPayload,
  ToolResultPayload,
} from '@/types'

export function dispatchStreamEvent(e: SSEEvent): void {
  const store = useChatStore.getState()
  switch (e.type) {
    case 'token': {
      // 逐 token 增量：聚合进流式消息（content 或 reasoning）；首个 token 同时
      // 顶替附件解析进度徽标（"正在解析附件…"使命完成）
      const p = e.payload as unknown as TokenPayload
      store.setParseStatus(null)
      store.appendToken(e.agent_path ?? 'root', p.id, p.delta, !!p.reasoning)
      break
    }
    case 'status': {
      // 附件预消费进度（非错误型）：parsing/skipped 映射临时徽标；parsed 不展示
      //（首个 token 即将到达，避免徽标闪烁）
      const p = e.payload as unknown as StatusPayload
      if (p.phase === 'parsing') {
        const names = (p.files ?? []).join('、')
        store.setParseStatus(names ? `正在解析附件（${names}）…` : '正在解析附件…')
      } else if (p.phase === 'skipped') {
        store.setParseStatus(`「${p.file ?? '附件'}」解析失败已跳过${p.reason ? `：${p.reason}` : ''}`)
      } else {
        store.setParseStatus(null)
      }
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
      store.setParseStatus(null)
      store.setInterrupt(e.payload as unknown as InterruptPayload)
      break
    case 'done':
      store.finalizeStreaming()
      store.setParseStatus(null)
      store.clearAttachments()
      store.setStatus('idle')
      store.setStreamSource(null)
      break
    case 'error': {
      const p = e.payload as { message?: string }
      store.finalizeStreaming()
      store.setParseStatus(null)
      store.setStreamError(p.message ?? '会话出错')
      break
    }
    default:
      break
  }
}
