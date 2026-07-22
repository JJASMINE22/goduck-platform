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

  // —— 动作 ——
  pushUser: (content: string) => void
  pushAgent: (agentPath: string, content: string) => void
  upsertToolCall: (tc: Omit<ToolCallEntry, 'hasResult' | 'createdAt'>) => void
  setToolResult: (toolCallId: string, result: string) => void
  setStatus: (s: ChatStreamStatus) => void
  setInterrupt: (payload: InterruptPayload) => void
  clearInterrupt: () => void
  setStreamError: (msg: string | null) => void
  addAttachment: (u: UploadOut) => void
  removeAttachment: (uploadId: string) => void
  clearAttachments: () => void
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
}

let idSeq = 0
const genId = () => `m${Date.now()}-${idSeq++}`

export const useChatStore = create<ChatState>((set) => ({
  ...initialState,

  pushUser: (content) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'user', agentPath: 'user', content, createdAt: Date.now() },
      ],
    })),

  pushAgent: (agentPath, content) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'agent', agentPath, content, createdAt: Date.now() },
      ],
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

  loadHistory: (msgs) => set({ messages: msgs, toolCalls: [], status: 'idle', pendingInterrupt: null, streamError: null }),

  reset: () => set({ ...initialState }),
}))
