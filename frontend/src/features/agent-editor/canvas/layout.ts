/* dagre 层级自动布局 —— 委派树自上而下排布。
   仅在出现「无坐标的新节点」时对整树重排；已有坐标的节点保留手动拖拽位置（由画布层控制）。
   dagre 给出节点中心坐标，xyflow 需左上角，故减去节点半宽/半高。 */
import dagre from '@dagrejs/dagre'
import type { AgentGraphNode } from '../graphModel'

export interface XYPos {
  x: number
  y: number
}

const NODE_W = 210
const NODE_H = 84

/** 对整棵树做层级布局，返回 { nodeId -> 左上角坐标 } */
export function layoutTree(nodes: AgentGraphNode[]): Record<string, XYPos> {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'TB', nodesep: 56, ranksep: 90, marginx: 24, marginy: 24 })
  g.setDefaultEdgeLabel(() => ({}))

  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }))
  nodes.forEach((n) => {
    if (n.parentId) g.setEdge(n.parentId, n.id)
  })

  dagre.layout(g)

  const pos: Record<string, XYPos> = {}
  nodes.forEach((n) => {
    const gn = g.node(n.id)
    if (gn) pos[n.id] = { x: gn.x - NODE_W / 2, y: gn.y - NODE_H / 2 }
  })
  return pos
}

export const NODE_SIZE = { width: NODE_W, height: NODE_H }
