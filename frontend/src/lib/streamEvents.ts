/* 流式事件 → chatStore 动作的共享分发器。
   两个来源共用同一映射（准则：单一消费路径，视图行为一致）：
   - SSE（useStreamChat：POST /messages、/resume）；
   - 语音 WS 下行隧道（useVoiceInput：stt_output 触发 astream 后的 agent 事件）。
   注意：done/error 的"终态收尾"（clearAttachments 等）在此统一处理，
   与 useStreamChat 原实现行为逐帧一致。

   性能：token 事件先入模块级缓冲、按窗口（TOKEN_COALESCE_MS）合并落地，
   而非逐事件写 store + 触发渲染——LLM 流可达 50–100 token/s，逐 token 全量
   渲染是长会话吐字卡顿的主因。合并只改变落地节奏（≈30fps 批次），不改变
   内容、到达顺序与状态机语义：
   - 任何非 token 事件处理前先冲刷（flushPendingTokens），store 看到的事件
     顺序与网络到达顺序严格一致（终态 finalizeStreaming 含上最后一批增量；
     tool_call/agent_message 等时间线条目不会插到未落地 token 之前）；
   - 中止/卸载路径（useStreamChat.cancel、语音掉线恢复）显式冲刷后再固化。 */
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
  UsagePayload,
} from '@/types'

/** token 合并窗口（ms）：≈30fps，吐字观感与逐 token 无差别 */
const TOKEN_COALESCE_MS = 32

interface PendingToken {
  agentPath: string
  tokenId: string
  delta: string
  reasoning: boolean
}

/** 模块级缓冲（SSE 与语音 WS 共用同一分发器，天然共享同一缓冲，且同一时刻
    至多一路流活跃——状态机保证）。到达顺序完整保留。 */
let pendingTokens: PendingToken[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null

/** 立即冲刷缓冲中的 token（幂等，可安全重入）。
    终态/中止/卸载路径必须先调用：finalizeStreaming 之后才落地的增量只会
    徒增一次已固化消息的 Markdown 重解析。 */
export function flushPendingTokens(): void {
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  if (!pendingTokens.length) return
  // 相邻同源（同 tokenId + 同类型）delta 预先拼接，减少 store 写入次数；
  // 非相邻的交错流（多代理并行）保持独立调用，时间线顺序不变
  const batch: PendingToken[] = []
  for (const t of pendingTokens) {
    const last = batch[batch.length - 1]
    if (last && last.tokenId === t.tokenId && last.reasoning === t.reasoning) {
      last.delta += t.delta
    } else {
      batch.push(t)
    }
  }
  pendingTokens = []
  const store = useChatStore.getState()
  // 原"逐 token 顶替附件解析进度徽标"收敛为每批一次（首 token 批即清）
  store.setParseStatus(null)
  for (const t of batch) store.appendToken(t.agentPath, t.tokenId, t.delta, t.reasoning)
}

function bufferToken(t: PendingToken): void {
  pendingTokens.push(t)
  if (flushTimer === null) {
    // 定时器锚定在本批首个 token：窗口有界，不会随持续到达无限顺延
    flushTimer = setTimeout(() => {
      flushTimer = null
      flushPendingTokens()
    }, TOKEN_COALESCE_MS)
  }
}

export function dispatchStreamEvent(e: SSEEvent): void {
  const store = useChatStore.getState()
  // 观察流水位记录（重挂增量对齐用；POST/GET/语音三路事件同源记录）
  if (e.id) store.setLastEventId(e.id)
  // token 入缓冲延后落地；其余事件先冲刷，保证顺序与到达严格一致（见文件头）
  if (e.type === 'token') {
    const p = e.payload as unknown as TokenPayload
    bufferToken({
      agentPath: e.agent_path ?? 'root',
      tokenId: p.id,
      delta: p.delta,
      reasoning: !!p.reasoning,
    })
    return
  }
  flushPendingTokens()
  // 观察流重挂（GET 重放）时 store 可能仍在 idle：内容事件到达即进入
  // streaming，输入锁与「生成中」状态随重放恢复（POST/语音路径事件到达前
  // 已置 streaming，此处守卫天然空转）
  if (
    store.status === 'idle' &&
    (e.type === 'agent_message' || e.type === 'thinking' || e.type === 'tool_call' ||
      e.type === 'tool_result' || e.type === 'todos' || e.type === 'file_change' ||
      e.type === 'usage')
  ) {
    store.setStatus('streaming')
    store.setStreamSource('sse')
  }
  switch (e.type) {
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
      store.setToolResult(p.tool_call_id, p.content, p.jev_kb)
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
    case 'usage': {
      // token 用量（一次 LLM 调用）：累加进会话级累计徽标
      store.applyUsageIncrement(e.payload as unknown as UsagePayload)
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
    case 'canceled':
      // run 被显式取消（本页 / 他处触发 POST /cancel、语音 cancel 帧）：
      // 与手动中止同落态（markTurnInterrupted 幂等，不叠加提示条）
      store.finalizeStreaming()
      store.setParseStatus(null)
      store.clearTodos()
      store.markTurnInterrupted()
      store.setStatus('idle')
      store.setStreamSource(null)
      break
    default:
      break
  }
}
