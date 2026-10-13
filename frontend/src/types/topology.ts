/** 后端 TopologyGraph（schemas.py）—— GET /agents/{id}/topology.json 响应。
 *  与 topology.mmd 同源（AgentConfig 树）但保留结构，React Flow 直接消费。 */
import type { UUID } from './common'

export type TopologyRole = 'main' | 'sub' | 'ghost'
export type TopologyEdgeKind = 'task' | 'auto'

export interface TopologyNodeCaps {
  tools: string[]
  skills: string[]
  hitl: string[]
  memory: boolean
  workspace: boolean
  shell: boolean
  rubric: boolean
  task_planning: boolean
}

export interface TopologyNode {
  id: string
  role: TopologyRole
  /** 子代理名；main/ghost 为 null（前端给展示文案） */
  name?: string | null
  /** 单行摘要：main=system_prompt 首行，sub=description 首行 */
  summary?: string | null
  depth: number
  is_dicted: boolean
  dynamic_dispatch: boolean
  caps: TopologyNodeCaps
}

export interface TopologyEdge {
  id: string
  source: string
  target: string
  kind: TopologyEdgeKind
}

export interface TopologyGraph {
  agent_id: UUID
  agent_name: string
  model_name?: string | null
  usage: 'chat' | 'cron'
  nodes: TopologyNode[]
  edges: TopologyEdge[]
}
