/* 聊天会话状态（Zustand）—— 流式消息流不进 TanStack Query（增量不适合缓存）。
   状态机以 SSE 终止事件为唯一终态来源（DESIGN 准则 F3）。
   消息模型比后端 MessageOut 更丰富：支持流式 agent 消息、工具卡配对、HITL 待决。 */
import { create } from 'zustand'
import type {
  ChatStreamStatus,
  InterruptPayload,
  UploadOut,
} from '@/types'

// —— 前端聊天视图模型（非后端契约，仅 UI 层）——
export interface ChatMessage {
  id: string
  role: 'user' | 'agent'
  agentPath: string // "root" | "root/researcher"
  content: string
  reasoning?: string // 思考过程（仅 agent 思考消息，可折叠展示）
  /** true=仍在 token 流式接收中（按纯文本渲染，避免逐 token 重解析 Markdown） */
  streaming?: boolean
  /** 用户消息附带的附件（发送时快照；刷新后历史不回显——后端无 message↔upload 关联） */
  attachments?: UploadOut[]
  createdAt: number
}

export interface ToolCallEntry {
  id: string // tool_call id（来自 tool_call 事件）
  name: string
  args: Record<string, unknown>
  caller?: string | null
  agentPath: string
  result?: string // tool_result 回填
  hasResult: boolean
  createdAt: number
}

export interface PendingInterrupt {
  payload: InterruptPayload
  arrivedAt: number
}

export interface ChatState {
  // —— 数据 ——
  messages: ChatMessage[]
  toolCalls: ToolCallEntry[]
  status: ChatStreamStatus
  pendingInterrupt: PendingInterrupt | null
  attachments: UploadOut[] // 待发附件，发消息后清空
  streamError: string | null
  // 发送框草稿：上提到 store，使回撤可回填原文（受控于 store，非组件局部 state）
  draft: string

  // —— 动作 ——
  pushUser: (content: string, attachments?: UploadOut[]) => void
  pushAgent: (agentPath: string, content: string) => void
  pushReasoning: (agentPath: string, reasoning: string) => void
  /** 追加一个 token 增量：按 (tokenId, reasoning) 聚合到既有流式消息或新建一条 */
  appendToken: (agentPath: string, tokenId: string, delta: string, reasoning: boolean) => void
  /** 结束流式：把所有 streaming 消息固化为可 Markdown 渲染 */
  finalizeStreaming: () => void
  upsertToolCall: (tc: Omit<ToolCallEntry, 'hasResult' | 'createdAt'>) => void
  setToolResult: (toolCallId: string, result: string) => void
  setStatus: (s: ChatStreamStatus) => void
  setInterrupt: (payload: InterruptPayload) => void
  clearInterrupt: () => void
  setStreamError: (msg: string | null) => void
  addAttachment: (u: UploadOut) => void
  removeAttachment: (uploadId: string) => void
  clearAttachments: () => void
  setAttachments: (list: UploadOut[]) => void
  setDraft: (v: string) => void
  /** 乐观移除一个完整回合（用户消息 + 其后到下一用户消息前的全部 AI/工具消息）
      及同时间窗内的工具调用卡；真值以后端历史重拉兜底（准则 R9） */
  removeTurn: (messageId: string) => void
  loadHistory: (msgs: ChatMessage[]) => void // 从后端 history 灌入后重置
  reset: () => void
}

const initialState = {
  messages: [] as ChatMessage[],
  toolCalls: [] as ToolCallEntry[],
  status: 'idle' as ChatStreamStatus,
  pendingInterrupt: null as PendingInterrupt | null,
  attachments: [] as UploadOut[],
  streamError: null as string | null,
  draft: '' as string,
}

let idSeq = 0
const genId = () => `m${Date.now()}-${idSeq++}`

export const useChatStore = create<ChatState>((set) => ({
  ...initialState,

  pushUser: (content, attachments) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'user', agentPath: 'user', content, attachments, createdAt: Date.now() },
      ],
    })),

  pushAgent: (agentPath, content) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'agent', agentPath, content, createdAt: Date.now() },
      ],
    })),

  pushReasoning: (agentPath, reasoning) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'agent', agentPath, content: '', reasoning, createdAt: Date.now() },
      ],
    })),

  appendToken: (agentPath, tokenId, delta, reasoning) =>
    set((s) => {
      // content 与 reasoning 即便共享同一条 LLM 消息 id，也作为独立条目聚合
      const msgId = reasoning ? `${tokenId}#r` : `${tokenId}#c`
      const idx = s.messages.findIndex((m) => m.id === msgId)
      if (idx >= 0) {
        const next = [...s.messages]
        const m = next[idx]
        next[idx] = reasoning
          ? { ...m, reasoning: (m.reasoning ?? '') + delta }
          : { ...m, content: (m.content ?? '') + delta }
        return { messages: next }
      }
      // 新流式条目：先把上一条仍在流式的固化（转 Markdown 渲染）
      const finalized = s.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m))
      const created: ChatMessage = reasoning
        ? { id: msgId, role: 'agent', agentPath, content: '', reasoning: delta, streaming: true, createdAt: Date.now() }
        : { id: msgId, role: 'agent', agentPath, content: delta, streaming: true, createdAt: Date.now() }
      return { messages: [...finalized, created] }
    }),

  finalizeStreaming: () =>
    set((s) => ({
      messages: s.messages.some((m) => m.streaming)
        ? s.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m))
        : s.messages,
    })),

  upsertToolCall: (tc) =>
    set((s) => {
      const idx = s.toolCalls.findIndex((t) => t.id === tc.id)
      const entry: ToolCallEntry = { ...tc, hasResult: false, createdAt: Date.now() }
      if (idx >= 0) {
        const next = [...s.toolCalls]
        next[idx] = { ...next[idx], ...entry }
        return { toolCalls: next }
      }
      return { toolCalls: [...s.toolCalls, entry] }
    }),

  setToolResult: (toolCallId, result) =>
    set((s) => ({
      toolCalls: s.toolCalls.map((t) =>
        t.id === toolCallId ? { ...t, result, hasResult: true } : t,
      ),
    })),

  setStatus: (status) => set({ status }),

  setInterrupt: (payload) =>
    set({ pendingInterrupt: { payload, arrivedAt: Date.now() }, status: 'interrupted' }),

  clearInterrupt: () => set({ pendingInterrupt: null }),

  setStreamError: (msg) => set({ streamError: msg, status: msg ? 'error' : 'idle' }),

  addAttachment: (u) => set((s) => ({ attachments: [...s.attachments, u] })),

  removeAttachment: (uploadId) =>
    set((s) => ({ attachments: s.attachments.filter((a) => a.upload_id !== uploadId) })),

  clearAttachments: () => set({ attachments: [] }),

  setAttachments: (list) => set({ attachments: list }),

  setDraft: (v) => set({ draft: v }),

  removeTurn: (messageId) =>
    set((s) => {
      const idx = s.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return {}
      // 连续块 [idx, 下一用户消息)：整段删除，保证 tool_call 配对完整
      let end = idx + 1
      while (end < s.messages.length && s.messages[end].role !== 'user') end++
      const turnStart = s.messages[idx].createdAt
      const turnEnd =
        end < s.messages.length ? s.messages[end].createdAt : Number.MAX_SAFE_INTEGER
      return {
        messages: [...s.messages.slice(0, idx), ...s.messages.slice(end)],
        // 同时间窗内的工具调用卡一并移除（timeline 合并视图两处需同步）
        toolCalls: s.toolCalls.filter((t) => t.createdAt < turnStart || t.createdAt >= turnEnd),
      }
    }),

  loadHistory: (msgs) => set({ messages: msgs, toolCalls: [], status: 'idle', pendingInterrupt: null, streamError: null }),

  reset: () => set({ ...initialState }),
}))
