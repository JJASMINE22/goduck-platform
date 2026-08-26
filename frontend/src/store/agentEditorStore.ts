/* 智能体编辑器草稿状态（Zustand + immer）—— 画布编辑期单一真源为「归一化节点数组」。
   对应后端「两类历史区分」：草稿丢弃不影响已存配置；保存才落库（DESIGN 准则 F8）。
   后端契约仍是递归 AgentConfig 树，故保存/校验前用 toConfigTree(nodes) 序列化回去。

   归一化模型让 xyflow 的 nodes/edges 与真源天然对齐；增删/换父级/重排都是 id+parentId 操作。 */
import { create } from 'zustand'
import { immer } from 'zustand/middleware/immer'
import type { AgentDetail, UUID } from '@/types'
import {
  canReparent,
  descendantIds,
  fromConfigTree,
  newMainNode,
  newSubagentNode,
  nextOrder,
  type AgentGraphNode,
  type NodeFields,
} from '@/features/agent-editor/graphModel'

interface AgentEditorState {
  // —— 草稿数据（归一化图模型）——
  nodes: AgentGraphNode[] | null // null = 未初始化
  selectedNodeId: string | null
  agentId: UUID | null // null = 新建模式
  name: string // 智能体名（外层，非节点 name）
  description: string
  modelId: UUID | null
  /** 下挂视觉模型（基座无视觉时的图像预消费）；基座自带视觉时须为 null（后端 400 互斥） */
  visionModelId: UUID | null
  dirty: boolean
  /** 用户是否改动过「配置树」（节点增删 / 字段编辑 / 换父级）。
   *  顶层名称/描述/模型绑定不算配置改动，不置位。
   *  用作实时校验的触发门控：未改动配置时不打 /validate，避免空表单红条闪烁与无谓请求（准则 F6）。 */
  configDirty: boolean

  // —— 校验/预览（来自 POST /agents/validate，不落库）——
  validationErrors: string[]
  mermaidPreview: string | null

  // —— 动作 ——
  initNew: () => void
  loadFromAgent: (detail: AgentDetail) => void
  selectNode: (id: string | null) => void
  /** 新增子代理节点；返回新节点 id（供画布定位） */
  addNode: (parentId: string, partial?: Partial<NodeFields>) => string
  /** 删除节点及其全部后代（根不可删） */
  removeNode: (id: string) => void
  /** 就地合并字段 */
  updateFields: (id: string, patch: Partial<NodeFields>) => void
  /** 换父级（重连）。守卫：根不动、自连/环阻断 */
  reparent: (id: string, newParentId: string) => void
  setName: (v: string) => void
  setDescription: (v: string) => void
  setModelId: (v: UUID | null) => void
  setVisionModelId: (v: UUID | null) => void
  setValidation: (errors: string[], mermaid: string | null) => void
  reset: () => void
}

export const useAgentEditorStore = create<AgentEditorState>()(
  immer((set, get) => ({
    nodes: null,
    selectedNodeId: null,
    agentId: null,
    name: '',
    description: '',
    modelId: null,
    visionModelId: null,
    dirty: false,
    configDirty: false,
    validationErrors: [],
    mermaidPreview: null,

    initNew: () =>
      set((s) => {
        const root = newMainNode()
        s.nodes = [root]
        s.agentId = null
        s.name = ''
        s.description = ''
        s.modelId = null
        s.visionModelId = null
        s.selectedNodeId = root.id
        s.dirty = false
        s.configDirty = false
        s.validationErrors = []
        s.mermaidPreview = null
      }),

    loadFromAgent: (detail) =>
      set((s) => {
        const nodes = fromConfigTree(detail.config_json)
        const root = nodes.find((n) => n.parentId === null) ?? null
        s.nodes = nodes
        s.agentId = detail.id
        s.name = detail.name
        s.description = detail.description ?? ''
        s.modelId = detail.model_id ?? null
        s.visionModelId = detail.vision_model_id ?? null
        s.selectedNodeId = root?.id ?? null
        s.dirty = false
        s.configDirty = false
        s.validationErrors = []
        s.mermaidPreview = detail.active_mermaid ?? null
      }),

    selectNode: (id) =>
      set((s) => {
        s.selectedNodeId = id
      }),

    addNode: (parentId, partial) => {
      const cur = get().nodes
      if (!cur) return ''
      const order = nextOrder(cur, parentId)
      const node = newSubagentNode(parentId, order, partial)
      set((s) => {
        if (!s.nodes) return
        s.nodes.push(node)
        s.selectedNodeId = node.id
        s.dirty = true
        s.configDirty = true
      })
      return node.id
    },

    removeNode: (id) => {
      const cur = get().nodes
      if (!cur) return
      const target = cur.find((x) => x.id === id)
      if (!target || target.parentId === null) return // 根不可删
      const removeSet = descendantIds(cur, id)
      const rootId = cur.find((x) => x.parentId === null)?.id ?? null
      set((s) => {
        if (!s.nodes) return
        s.nodes = s.nodes.filter((x) => !removeSet.has(x.id))
        if (s.selectedNodeId && removeSet.has(s.selectedNodeId)) {
          s.selectedNodeId = rootId
        }
        s.dirty = true
        s.configDirty = true
      })
    },

    updateFields: (id, patch) =>
      set((s) => {
        const n = s.nodes?.find((x) => x.id === id)
        if (n) {
          Object.assign(n, patch)
          s.dirty = true
          s.configDirty = true
        }
      }),

    reparent: (id, newParentId) => {
      const cur = get().nodes
      if (!cur || !canReparent(cur, id, newParentId)) return // 规则统一收敛在 canReparent
      const order = nextOrder(cur, newParentId)
      set((s) => {
        const n = s.nodes?.find((x) => x.id === id)
        if (n) {
          n.parentId = newParentId
          n.order = order
          s.dirty = true
          s.configDirty = true
        }
      })
    },

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

    setVisionModelId: (v) =>
      set((s) => {
        s.visionModelId = v
        s.dirty = true
      }),

    setValidation: (errors, mermaid) =>
      set((s) => {
        s.validationErrors = errors
        s.mermaidPreview = mermaid
      }),

    reset: () =>
      set((s) => {
        s.nodes = null
        s.agentId = null
        s.name = ''
        s.description = ''
        s.modelId = null
        s.visionModelId = null
        s.selectedNodeId = null
        s.dirty = false
        s.configDirty = false
        s.validationErrors = []
        s.mermaidPreview = null
      }),
  })),
)
