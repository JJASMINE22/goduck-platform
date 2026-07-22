/* AgentConfig 树节点辅助 —— 主代理 path=[]，子代理 path=[i,...]（subagents 索引序列）。
   getNode 对 immer draft 同样适用（proxy 透传），便于就地修改。 */
import type { AgentConfig } from '@/types'

export type NodePath = number[]

export function getNode(cfg: AgentConfig, path: NodePath): AgentConfig {
  let n: AgentConfig = cfg
  for (const i of path) {
    n = n.subagents?.[i] ?? n
  }
  return n
}

export function newMainConfig(): AgentConfig {
  return { system_prompt: '', tool_names: [], is_main: true, subagents: [], allow_attachments: false }
}

export function newSubagent(): AgentConfig {
  return { name: '', description: '', system_prompt: '', tool_names: [], subagents: [] }
}

/** 默认 rubric（启用时） */
export function defaultRubric() {
  return { content: '', fixed: false, max_iterations: 3, grader_tool_names: [] }
}
