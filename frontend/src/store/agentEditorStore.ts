/* 智能体编辑器草稿状态（Zustand + immer）—— 编辑中的 AgentConfig 树未保存前在内存。
   对应后端「两类历史区分」：草稿丢弃不影响已存配置；保存才落库（DESIGN 准则 F8）。
   immer 让递归 subagents 树的局部更新更可读。 */
import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'
import type { AgentConfig, AgentDetail, UUID } from '@/types'

/** 新建智能体的默认主代理草稿 */
function defaultMainConfig(): AgentConfig {
  return {
    system_prompt: '',
    tool_names: [],
    is_main: true,
    subagents: [],
    allow_attachments: false,
  }
}

interface AgentEditorState {
  // —— 草稿数据 ——
  config: AgentConfig | null
  agentId: UUID | null // null = 新建模式
  name: string // 智能体名（外层，非 AgentConfig.name）
  description: string
  modelId: UUID | null
  dirty: boolean

  // —— 校验/预览（来自 POST /agents/validate，不落库）——
  validationErrors: string[]
  mermaidPreview: string | null

  // —— 动作 ——
  initNew: () => void
  loadFromAgent: (detail: AgentDetail) => void
  /** immer 风格就地修改 config 树（递归子树更新的统一入口） */
  mutate: (fn: (draft: AgentConfig) => void) => void
  replaceConfig: (config: AgentConfig) => void
  setName: (v: string) => void
  setDescription: (v: string) => void
  setModelId: (v: UUID | null) => void
  markClean: () => void
  setValidation: (errors: string[], mermaid: string | null) => void
  reset: () => void
}

export const useAgentEditorStore = create<AgentEditorState>()(
  immer((set) => ({
    config: null,
    agentId: null,
    name: '',
    description: '',
    modelId: null,
    dirty: false,
    validationErrors: [],
    mermaidPreview: null,

    initNew: () =>
      set((s) => {
        s.config = defaultMainConfig()
        s.agentId = null
        s.name = ''
        s.description = ''
        s.modelId = null
        s.dirty = false
        s.validationErrors = []
        s.mermaidPreview = null
      }),

    loadFromAgent: (detail) =>
      set((s) => {
        s.config = structuredClone(detail.config_json)
        s.agentId = detail.id
        s.name = detail.name
        s.description = detail.description ?? ''
        s.modelId = detail.model_id ?? null
        s.dirty = false
        s.validationErrors = []
        s.mermaidPreview = detail.active_mermaid ?? null
      }),

    mutate: (fn) =>
      set((s) => {
        if (!s.config) return
        fn(s.config)
        s.dirty = true
      }),

    replaceConfig: (config) =>
      set((s) => {
        s.config = config
        s.dirty = true
      }),

    setName: (v) =>
      set((s) => {
        s.name = v
        s.dirty = true
      }),

    setDescription: (v) =>
      set((s) => {
        s.description = v
        s.dirty = true
      }),

    setModelId: (v) =>
      set((s) => {
        s.modelId = v
        s.dirty = true
      }),

    markClean: () =>
      set((s) => {
        s.dirty = false
      }),

    setValidation: (errors, mermaid) =>
      set((s) => {
        s.validationErrors = errors
        s.mermaidPreview = mermaid
      }),

    reset: () =>
      set((s) => {
        s.config = null
        s.agentId = null
        s.name = ''
        s.description = ''
        s.modelId = null
        s.dirty = false
        s.validationErrors = []
        s.mermaidPreview = null
      }),
  })),
)
