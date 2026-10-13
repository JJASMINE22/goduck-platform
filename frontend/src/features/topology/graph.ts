/* 拓扑图纯逻辑 —— React Flow 数据派生（dagre TB 布局）与邻接计算。
    无 React 运行时依赖，可单测；高亮集两种语义：
    悬停 = intent-trace（自身 + 直接邻居），聚焦 = 链路（祖先 + 全部子孙）。 */
import dagre from '@dagrejs/dagre'
import { MarkerType, type Edge } from '@xyflow/react'
import type { CSSProperties } from 'react'
import type { TopologyEdge, TopologyGraph, TopologyNode } from '@/types'
import type { TopoFlowNode } from './TopologyNodeCard'

/** 预览节点卡尺寸（紧凑两行：头带 + 摘要/计数；内边距加宽后 70px 高） */
export const TOPO_NODE_SIZE = { width: 176, height: 70 }
const TOPO_EDGE_TYPE = 'smoothstep'

export interface XYPos {
  x: number
  y: number
}

/** dagre TB 层级布局：返回 nodeId → 左上角坐标（dagre 给中心，需减半宽高）。 */
export function layoutTopology(nodes: TopologyNode[], edges: TopologyEdge[]): Record<string, XYPos> {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 40, ranksep: 72, marginx: 20, marginy: 20 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of nodes) g.setNode(n.id, { width: TOPO_NODE_SIZE.width, height: TOPO_NODE_SIZE.height })
  for (const e of edges) g.setEdge(e.source, e.target)
  dagre.layout(g)
  const pos: Record<string, XYPos> = {}
  for (const n of nodes) {
    const gn = g.node(n.id)
    if (gn) pos[n.id] = { x: gn.x - TOPO_NODE_SIZE.width / 2, y: gn.y - TOPO_NODE_SIZE.height / 2 }
  }
  return pos
}

/** 图索引：邻接/父子关系一次构建，交互高亮集与详情查询共用。 */
export interface TopologyIndex {
  byId: Map<string, TopologyNode>
  /** 无向直接邻居（intent-trace 用） */
  neighbors: Map<string, Set<string>>
  parentOf: Map<string, string>
  childrenOf: Map<string, Set<string>>
}

export function indexGraph(graph: TopologyGraph): TopologyIndex {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]))
  const neighbors = new Map<string, Set<string>>()
  const parentOf = new Map<string, string>()
  const childrenOf = new Map<string, Set<string>>()
  const touch = (m: Map<string, Set<string>>, k: string): Set<string> => {
    let s = m.get(k)
    if (!s) m.set(k, (s = new Set()))
    return s
  }
  for (const e of graph.edges) {
    touch(neighbors, e.source).add(e.target)
    touch(neighbors, e.target).add(e.source)
    parentOf.set(e.target, e.source)
    touch(childrenOf, e.source).add(e.target)
  }
  return { byId, neighbors, parentOf, childrenOf }
}

/** 聚焦集 = 自身 + 祖先链（谁能委派到它）+ 全部子孙（它能委派到谁）。 */
export function focusSet(index: TopologyIndex, id: string): Set<string> {
  const out = new Set([id])
  for (let p = index.parentOf.get(id); p; p = index.parentOf.get(p)) out.add(p)
  const walk = (nid: string): void => {
    for (const c of index.childrenOf.get(nid) ?? []) {
      if (!out.has(c)) {
        out.add(c)
        walk(c)
      }
    }
  }
  walk(id)
  return out
}

/** 悬停集 = 自身 + 直接邻居（一眼看清直接委派关系）。 */
export function hoverSet(index: TopologyIndex, id: string): Set<string> {
  return new Set([id, ...(index.neighbors.get(id) ?? [])])
}

/** 图深度（行数）：根为第 1 行。用于画布高度自适应（单智能体不留大片空白）。 */
export function graphRows(graph: TopologyGraph): number {
  let max = 0
  for (const n of graph.nodes) if (n.depth > max) max = n.depth
  return max + 1
}

/** 派生 RF 节点（含 dagre 布局）。仅依赖 graph——悬停/聚焦变化时保持引用稳定，
    避免 RF 全量重渲节点（data 引用变化是悬浮抖动的来源之一；视觉态走 Context）。 */
export function toFlowNodes(graph: TopologyGraph): TopoFlowNode[] {
  const positions = layoutTopology(graph.nodes, graph.edges)
  return graph.nodes.map((n) => ({
    id: n.id,
    type: 'topo' as const,
    position: positions[n.id] ?? { x: 0, y: 0 },
    data: { node: n },
    style: { width: TOPO_NODE_SIZE.width, height: TOPO_NODE_SIZE.height },
  }))
}

/** 派生 RF 边。litIds 非空时：两端均点亮 → 流光 trace，否则压暗 dim。 */
export function toFlowEdges(graph: TopologyGraph, index: TopologyIndex, litIds: ReadonlySet<string> | null): Edge[] {
  return graph.edges.map((e) => {
    const cls = ['topo-edge', `topo-edge--${e.kind}`]
    if (litIds) {
      cls.push(litIds.has(e.source) && litIds.has(e.target) ? 'topo-edge--trace' : 'topo-edge--dim')
    }
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      type: TOPO_EDGE_TYPE,
      className: cls.join(' '),
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: 'var(--color-border)' },
      // 边深度 = 目标节点深度：ambient 流光按层错峰（archify --step × 160ms 同款）
      style: { '--topo-depth': String(index.byId.get(e.target)?.depth ?? 0) } as CSSProperties,
    }
  })
}
