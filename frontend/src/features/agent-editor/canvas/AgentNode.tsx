/* 画布节点卡 —— xyflow 自定义节点（type='agent'）。
   精简紧凑：左侧角色色条 + 发光圆点 + 名称 + 单行描述。
   主代理=紫色/根/不可删；子代理=青色。顶部 Handle 接父级连线，底部 Handle 连子级。 */
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { Bot, Boxes, Trash2 } from 'lucide-react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import type { AgentGraphNode } from '../graphModel'

export interface AgentNodeData {
  node: AgentGraphNode
  selected: boolean
  dynamic?: boolean
  [key: string]: unknown
}
export type AgentCanvasNode = Node<AgentNodeData, 'agent'>

export function AgentNodeCard({ id, data }: NodeProps<AgentCanvasNode>): React.ReactNode {
  const removeNode = useAgentEditorStore((s) => s.removeNode)
  const node = data.node
  const isMain = !!node.is_main
  const color = isMain ? 'var(--color-role-agent-main)' : 'var(--color-role-agent-sub)'
  const title = isMain ? '主代理' : node.name || '未命名子代理'
  const toolCount = node.tool_names?.length ?? 0
  // 主代理按 schema 无 description：改显 system_prompt 首行，避免误导性的"无描述"
  const descText = isMain
    ? (node.system_prompt?.trim().split('\n')[0] ?? '')
    : (node.description ?? '')

  return (
    <div
      className={`agent-node${data.selected ? ' agent-node--selected' : ''}${
        data.dynamic ? ' agent-node--dynamic' : ''
      }`}
      style={{ '--node-color': color } as React.CSSProperties}
    >
      <Handle type="target" position={Position.Top} className="agent-handle" />

      <div className="agent-node__head">
        <span className="agent-node__dot" />
        {isMain ? (
          <Bot size={13} className="agent-node__icon" />
        ) : (
          <Boxes size={13} className="agent-node__icon" />
        )}
        <span className="agent-node__title">{title}</span>
        {!isMain ? (
          <button
            type="button"
            className="agent-node__del"
            title="删除该子代理"
            onClick={(e) => {
              e.stopPropagation()
              removeNode(id)
            }}
          >
            <Trash2 size={12} />
          </button>
        ) : null}
      </div>

      <div className="agent-node__body">
        <p className={`agent-node__desc${descText ? '' : ' agent-node__desc--empty'}`}>
          {descText || (isMain ? '主代理网关' : '无描述')}
        </p>
        <div className="agent-node__meta">
          <Boxes size={11} />
          <span>{toolCount}</span>
          {node.is_dicted ? <span className="agent-node__dict">dict</span> : null}
        </div>
      </div>

      <Handle type="source" position={Position.Bottom} className="agent-handle" />
    </div>
  )
}
