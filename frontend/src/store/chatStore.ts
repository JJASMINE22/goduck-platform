/* 聊天会话状态（Zustand）—— 流式消息流不进 TanStack Query（增量不适合缓存）。
   状态机以 SSE 终止事件为唯一终态来源（DESIGN 准则 F3）。
   消息模型比后端 MessageOut 更丰富：支持流式 agent 消息、工具卡配对、HITL 待决。 */
import { create } from 'zustand'
import type {
  ChatStreamStatus,
  FileChangeRecord,
  InterruptPayload,
  KnowledgeBaseOut,
  TodoItem,
  UploadOut,
  VoiceState,
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
  /** true=该条为「用户已手动中断」提示条目（content 恒空，渲染为气泡下方提示行，
      不走 Markdown；仅存在于 live 会话，历史回放不重建——刷新后自然消失） */
  interrupted?: boolean
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
  /** true=本回合被手动中止时仍在执行（结果永不再到达）；重发后后端重放
      超步补齐结果时由 setToolResult 复位。展示为「已中断」而非永挂「执行中」 */
  abandoned?: boolean
  createdAt: number
}

export interface PendingInterrupt {
  payload: InterruptPayload
  arrivedAt: number
}

/** file_change 事件落地的一条记录（追加到达时间，供 removeTurn 时间窗过滤与时间线排序）。
    存储层保持逐条追加（同一文件多回合多次操作各自独立、可精确回撤）；
    同路径合并累计（create 保留 / delete 终态）在渲染层 useMemo 完成。 */
export interface FileChangeEntry extends FileChangeRecord {
  createdAt: number
}

export interface ChatState {
  // —— 数据 ——
  /** store 当前绑定的会话（loadHistory 绑定；同会话往返切页时据此保留 live 状态） */
  sessionId: string | null
  messages: ChatMessage[]
  toolCalls: ToolCallEntry[]
  status: ChatStreamStatus
  /** 当前 streaming 的来源：'sse'=手打（POST /messages|/resume），'voice'=语音 WS 隧道。
      中止/先停后撤需按来源路由——SSE abort fetch，语音发 WS cancel 帧（否则后端
      astream 照跑、withdraw 闸不放行，即语音回合无法中止/回撤的冲突根因）。
      仅 status==='streaming' 时有意义。 */
  streamSource: 'sse' | 'voice' | null
  pendingInterrupt: PendingInterrupt | null
  attachments: UploadOut[] // 待发附件，发消息后清空
  selectedKbs: KnowledgeBaseOut[] // 本次启用的知识库，发消息后清空
  streamError: string | null
  /** 各代理的任务规划清单（todos 事件整表写入，agent_path 键控）。
      回合作用域：pushUser 新回合清空、cancel 中止拆除——清单是对"当前请求"的
      执行进度，跨回合悬挂会导致旧任务清单累计不清；HITL resume 属同回合继续，
      保留（后端 graph state 中的 todos 仍跨回合持久，与前端展示生命周期解耦） */
  todosByAgent: Record<string, TodoItem[]>
  /** 本回合的工作区文件变更（file_change 事件追加；pushUser 新回合清空，回撤随时间窗移除） */
  fileChanges: FileChangeEntry[]
  // 发送框草稿：上提到 store，使回撤可回填原文（受控于 store，非组件局部 state）
  draft: string
  // —— 语音输入（麦克风小状态机，与 status 正交联动）——
  /** 'idle'=可按压；'recording'=长按采麦中；'transcribing'=松开等终稿 */
  voiceState: VoiceState
  /** stt_chunk 实时中间稿（录音期间流式渲染；stt_output 固化后清空） */
  interimTranscript: string

  // —— 动作 ——
  pushUser: (content: string, attachments?: UploadOut[]) => void
  pushAgent: (agentPath: string, content: string) => void
  pushReasoning: (agentPath: string, reasoning: string) => void
  /** 追加一个 token 增量：按 (tokenId, reasoning) 聚合到既有流式消息或新建一条 */
  appendToken: (agentPath: string, tokenId: string, delta: string, reasoning: boolean) => void
  /** 结束流式：把所有 streaming 消息固化为可 Markdown 渲染 */
  finalizeStreaming: () => void
  /** 手动中止收尾：在本回合时间线末尾落一条「用户已手动中断」提示条目。
      空回合（尚无任何 agent 输出/工具卡）不加，避免提示行孤悬在用户消息之后 */
  markTurnInterrupted: () => void
  upsertToolCall: (tc: Omit<ToolCallEntry, 'hasResult' | 'createdAt'>) => void
  setToolResult: (toolCallId: string, result: string) => void
  setStatus: (s: ChatStreamStatus) => void
  setStreamSource: (src: 'sse' | 'voice' | null) => void
  setInterrupt: (payload: InterruptPayload) => void
  clearInterrupt: () => void
  setStreamError: (msg: string | null) => void
  /** todos 事件落地：整表替换该 agent 的清单（后端已按变更去重） */
  setTodos: (agentPath: string, todos: TodoItem[]) => void
  /** 清空任务清单浮窗（新回合开始 / 手动中止时调用，防止旧清单跨回合悬挂累计） */
  clearTodos: () => void
  /** file_change 事件落地：同批记录统一时间戳追加（合并展示在渲染层完成） */
  applyFileChange: (records: FileChangeRecord[]) => void
  addAttachment: (u: UploadOut) => void
  removeAttachment: (uploadId: string) => void
  clearAttachments: () => void
  setAttachments: (list: UploadOut[]) => void
  addKb: (kb: KnowledgeBaseOut) => void
  removeKb: (kbId: string) => void
  clearKbs: () => void
  setKbs: (list: KnowledgeBaseOut[]) => void
  setDraft: (v: string) => void
  /** 语音状态机流转（idle→recording→transcribing→idle） */
  setVoiceState: (s: VoiceState) => void
  /** stt_chunk 中间稿整段替换（后端每 Turn 全量下发，非增量） */
  setInterimTranscript: (v: string) => void
  /** 乐观移除一个完整回合（用户消息 + 其后到下一用户消息前的全部 AI/工具消息）
      及同时间窗内的工具调用卡；真值以后端历史重拉兜底（准则 R9） */
  removeTurn: (messageId: string) => void
  /** 从后端 history 灌入（同时绑定会话标记；仅跨会话进入 / 显式失效时调用）。
      toolCalls 由 buildHistoryReplay 自同一份 history 重建（刷新后工具卡回显），
      缺省为空数组以兼容仅灌消息的调用方 */
  loadHistory: (sessionId: string, msgs: ChatMessage[], toolCalls?: ToolCallEntry[]) => void
  reset: () => void
}

const initialState = {
  sessionId: null as string | null,
  messages: [] as ChatMessage[],
  toolCalls: [] as ToolCallEntry[],
  status: 'idle' as ChatStreamStatus,
  streamSource: null as 'sse' | 'voice' | null,
  pendingInterrupt: null as PendingInterrupt | null,
  attachments: [] as UploadOut[],
  selectedKbs: [] as KnowledgeBaseOut[],
  streamError: null as string | null,
  todosByAgent: {} as Record<string, TodoItem[]>,
  fileChanges: [] as FileChangeEntry[],
  draft: '',
  voiceState: 'idle' as VoiceState,
  interimTranscript: '',
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
      // 新回合开始：上一回合的文件变更徽标随之消失（树/卡片同源清空）；
      // 任务清单浮窗同款清空——上一回合（尤其被中断的）计划不再悬挂，
      // 待本回合任一代理再次 write_todos 时重新出现
      fileChanges: [],
      todosByAgent: {},
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

  markTurnInterrupted: () =>
    set((s) => {
      // 定位最近一条用户消息：中断提示属于"该回合的收尾"，须落在本回合全部输出之后
      let lastUser = -1
      for (let i = s.messages.length - 1; i >= 0; i--) {
        if (s.messages[i].role === 'user') {
          lastUser = i
          break
        }
      }
      if (lastUser < 0) return {}
      const turnStart = s.messages[lastUser].createdAt
      const turnTools = s.toolCalls.filter((t) => t.createdAt >= turnStart)
      const hasOutput =
        s.messages.slice(lastUser + 1).some((m) => m.role === 'agent' && (m.content !== '' || !!m.reasoning)) ||
        turnTools.length > 0
      if (!hasOutput) return {}
      return {
        messages: [
          ...s.messages,
          { id: genId(), role: 'agent', agentPath: 'root', content: '', interrupted: true, createdAt: Date.now() },
        ],
        // 在途工具卡标记已中断：流已关闭，结果永不再到达，「执行中」不应永挂；
        // 若重发后后端重放超步补齐结果，setToolResult 会清掉该标记
        toolCalls: s.toolCalls.map((t) =>
          !t.hasResult && t.createdAt >= turnStart ? { ...t, abandoned: true } : t,
        ),
      }
    }),

  upsertToolCall: (tc) =>
    set((s) => {
      const idx = s.toolCalls.findIndex((t) => t.id === tc.id)
      if (idx >= 0) {
        // 同 id 重发（中止打断在超步提交前，重发后后端重放该超步）：保持卡片
        // 原有时序位置与结果状态，仅刷新调用元数据——若用 Date.now() 覆盖
        // createdAt，旧回合的工具卡会"传送"到新 query 之下（时序错乱的根因）
        const next = [...s.toolCalls]
        next[idx] = {
          ...next[idx],
          name: tc.name,
          args: tc.args,
          caller: tc.caller,
          agentPath: tc.agentPath,
        }
        return { toolCalls: next }
      }
      const entry: ToolCallEntry = { ...tc, hasResult: false, createdAt: Date.now() }
      return { toolCalls: [...s.toolCalls, entry] }
    }),

  setToolResult: (toolCallId, result) =>
    set((s) => ({
      toolCalls: s.toolCalls.map((t) =>
        t.id === toolCallId ? { ...t, result, hasResult: true, abandoned: false } : t,
      ),
    })),

  setStatus: (status) => set({ status }),

  setStreamSource: (streamSource) => set({ streamSource }),

  setInterrupt: (payload) =>
    set({ pendingInterrupt: { payload, arrivedAt: Date.now() }, status: 'interrupted' }),

  clearInterrupt: () => set({ pendingInterrupt: null }),

  setStreamError: (msg) => set({ streamError: msg, status: msg ? 'error' : 'idle' }),

  setTodos: (agentPath, todos) => set((s) => ({ todosByAgent: { ...s.todosByAgent, [agentPath]: todos } })),

  clearTodos: () => set({ todosByAgent: {} }),

  applyFileChange: (records) =>
    set((s) =>
      records.length
        ? { fileChanges: [...s.fileChanges, ...records.map((r) => ({ ...r, createdAt: Date.now() }))] }
        : {},
    ),

  addAttachment: (u) => set((s) => ({ attachments: [...s.attachments, u] })),

  removeAttachment: (uploadId) =>
    set((s) => ({ attachments: s.attachments.filter((a) => a.upload_id !== uploadId) })),

  clearAttachments: () => set({ attachments: [] }),

  setAttachments: (list) => set({ attachments: list }),

  addKb: (kb) => set((s) => (s.selectedKbs.some((k) => k.id === kb.id) ? {} : { selectedKbs: [...s.selectedKbs, kb] })),

  removeKb: (kbId) => set((s) => ({ selectedKbs: s.selectedKbs.filter((k) => k.id !== kbId) })),

  clearKbs: () => set({ selectedKbs: [] }),

  setKbs: (list) => set({ selectedKbs: list }),

  setDraft: (v) => set({ draft: v }),

  setVoiceState: (voiceState) => set({ voiceState }),

  setInterimTranscript: (interimTranscript) => set({ interimTranscript }),

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
        // 文件变更记录同窗移除：回撤回合时徽标卡片与树徽标随之消失
        fileChanges: s.fileChanges.filter((c) => c.createdAt < turnStart || c.createdAt >= turnEnd),
      }
    }),

  loadHistory: (sessionId, msgs, toolCalls = []) =>
    set({
      sessionId,
      messages: msgs,
      toolCalls,
      status: 'idle',
      streamSource: null,
      pendingInterrupt: null,
      streamError: null,
      todosByAgent: {},
      fileChanges: [],
    }),

  reset: () => set({ ...initialState }),
}))
