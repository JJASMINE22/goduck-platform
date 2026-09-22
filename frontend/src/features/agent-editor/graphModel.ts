/* 智能体归一化图模型 —— 画布编辑期的单一真源。
   后端契约是「递归 AgentConfig 树」（subagents 嵌套）；画布是「图」（节点+parentId 边）。
   故编辑期把树扁平成 AgentGraphNode[]（每节点带稳定 id + parentId + order），
   仅在加载/保存两边界用 fromConfigTree / toConfigTree 互转（DESIGN：单一真相 AgentConfig）。

   归一化的好处：xyflow 的 nodes/edges 与真源天然对齐；拖拽/换父级/重排都退化为
   parentId + order 操作，无需在递归树上找路径。 */
import type { AgentConfig } from '@/types'

/** 单个画布节点 = AgentConfig 去掉 subagents + 编辑期元数据(id/parentId/order) */
export type NodeFields = Omit<AgentConfig, 'subagents'>

export interface AgentGraphNode extends NodeFields {
  id: string
  parentId: string | null
  order: number
}

let _seq = 0
/** 生成稳定节点 id（同一棵树结构 → 同一序列，便于确定性；编辑期内不变） */
export function genNodeId(): string {
  return `n${(_seq++).toString(36)}`
}

/** 递归 AgentConfig 树 → 扁平节点数组（加载/灌历史用） */
export function fromConfigTree(config: AgentConfig): AgentGraphNode[] {
  const out: AgentGraphNode[] = []
  _seq = 0
  const walk = (cfg: AgentConfig, parentId: string | null, order: number): void => {
    const { subagents, ...fields } = cfg
    const id = genNodeId()
    out.push({ ...fields, id, parentId, order })
    ;(subagents ?? []).forEach((sa, i) => walk(sa, id, i))
  }
  walk(config, null, 0)
  return out
}

/** 扁平节点数组 → 递归 AgentConfig 树（保存/校验用；以最终 AgentConfig 为契约） */
export function toConfigTree(nodes: AgentGraphNode[]): AgentConfig {
  const childrenOf = (pid: string | null): AgentGraphNode[] =>
    nodes.filter((n) => n.parentId === pid).sort((a, b) => a.order - b.order)

  const build = (node: AgentGraphNode): AgentConfig => {
    const kids = childrenOf(node.id)
    const cfg: AgentConfig = {
      name: node.name,
      description: node.description,
      system_prompt: node.system_prompt,
      tool_names: node.tool_names,
      skills: node.skills,
      memory: node.memory,
      workspace: node.workspace,
      interrupt_on: node.interrupt_on,
      is_main: node.is_main,
      is_dicted: node.is_dicted,
      dynamic_dispatch: node.dynamic_dispatch,
      rubric: node.rubric,
      allow_attachments: node.allow_attachments,
      allow_knowledge: node.allow_knowledge,
      allow_shell: node.allow_shell,
      shell_guard: node.shell_guard,
      // 用途声明仅根节点携带（子代理 undefined → 序列化时丢弃）
      usage: node.parentId === null ? node.usage : undefined,
      task_planning: node.task_planning,
    }
    cfg.subagents = kids.length ? kids.map(build) : []
    return cfg
  }

  const root = nodes.find((n) => n.parentId === null)
  if (!root) throw new Error('toConfigTree: 缺少主代理（根）节点')
  return build(root)
}

/** 主代理（根）默认节点 */
export function newMainNode(): AgentGraphNode {
  return {
    id: genNodeId(),
    parentId: null,
    order: 0,
    system_prompt: '',
    tool_names: [],
    is_main: true,
    allow_attachments: false,
    allow_knowledge: false,
    allow_shell: false,
  }
}

/** 新建子代理节点（可选模板字段覆盖） */
export function newSubagentNode(
  parentId: string,
  order: number,
  partial?: Partial<NodeFields>,
): AgentGraphNode {
  return {
    id: genNodeId(),
    parentId,
    order,
    name: '',
    description: '',
    system_prompt: '',
    tool_names: [],
    ...partial,
  }
}

/** 计算某父级下的下一个 order 值（追加子节点用） */
export function nextOrder(nodes: AgentGraphNode[], parentId: string): number {
  return nodes.filter((n) => n.parentId === parentId).length
}

/** 收集某节点的全部后代 id（含自身），删除/校验循环用 */
export function descendantIds(nodes: AgentGraphNode[], id: string): Set<string> {
  const result = new Set<string>([id])
  let changed = true
  while (changed) {
    changed = false
    for (const n of nodes) {
      if (n.parentId && result.has(n.parentId) && !result.has(n.id)) {
        result.add(n.id)
        changed = true
      }
    }
  }
  return result
}

/** 是否存在 id（含其自身）—— 换父级前检测潜在环 */
export function isDescendantOrSelf(nodes: AgentGraphNode[], maybeChild: string, ancestor: string): boolean {
  return descendantIds(nodes, ancestor).has(maybeChild)
}

/** reparent 前置校验：子节点存在且非根、新父存在、非自身、新父不在子节点的子树内（防环）。
 *  画布连线高亮与 store.reparent 共用此规则，确保「提示可连」与「真正可连」判定一致。 */
export function canReparent(
  nodes: AgentGraphNode[],
  childId: string,
  newParentId: string,
): boolean {
  const child = nodes.find((n) => n.id === childId)
  if (!child || child.parentId === null) return false // 根不可换父
  if (childId === newParentId) return false
  if (!nodes.some((n) => n.id === newParentId)) return false
  return !isDescendantOrSelf(nodes, newParentId, childId)
}
