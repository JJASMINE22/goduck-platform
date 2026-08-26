/* 画布节点卡 —— xyflow 自定义节点（type='agent'）。
   精简紧凑：左侧角色色条 + 发光圆点 + 名称 + 单行描述。
   主代理=紫色/根/不可删/无顶部手柄（根永远不可能是子级）；子代理=青色。
   连线进行中：connecting 使手柄全量显形，connectRole 高亮本节点落点合法性，
   起点节点额外浮出 connectHint 语义提示。 */
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { Bot, Boxes, Trash2 } from 'lucide-react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import type { AgentGraphNode } from '../graphModel'

/** 连线进行中本节点的角色：起点 / 合法落点 / 非法落点（自身·后代·根） */
export type ConnectRole = 'none' | 'self' | 'valid' | 'invalid'

export interface AgentNodeData {
  node: AgentGraphNode
  selected: boolean
  dynamic?: boolean
  /** 画布级：当前有连线拖拽进行中 */
  connecting?: boolean
  /** 连线进行中本节点的落点合法性 */
  connectRole?: ConnectRole
  /** 起点节点的语义提示文案 */
  connectHint?: string
  [key: string]: unknown
}
export type AgentCanvasNode = Node<AgentNodeData, 'agent'>

export function AgentNodeCard({ id, data }: NodeProps<AgentCanvasNode>): React.ReactNode {
  const removeNode = useAgentEditorStore((s) => s.removeNode)
  const node = data.node
  const isRoot = node.parentId === null
  const isMain = !!node.is_main
  const color = isMain ? 'var(--color-role-agent-main)' : 'var(--color-role-agent-sub)'
  const title = isMain ? '主代理' : node.name || '未命名子代理'
  const toolCount = node.tool_names?.length ?? 0
  // 主代理按 schema 无 description：改显 system_prompt 首行，避免误导性的"无描述"
  const descText = isMain
    ? (node.system_prompt?.trim().split('\n')[0] ?? '')
    : (node.description ?? '')
  const connectRole = data.connectRole ?? 'none'
  const handleCls = `agent-handle${data.connecting ? ' agent-handle--active' : ''}`

  return (
    <div
      className={`agent-node${data.selected ? ' agent-node--selected' : ''}${
        data.dynamic ? ' agent-node--dynamic' : ''
      }${connectRole !== 'none' ? ` agent-node--connect-${connectRole}` : ''}`}
      style={{ '--node-color': color } as React.CSSProperties}
    >
      {isRoot ? null : (
        <Handle type="target" position={Position.Top} className={handleCls} />
      )}

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

      {connectRole === 'self' && data.connectHint ? (
        <span className="agent-node__connect-hint">{data.connectHint}</span>
      ) : null}

      <Handle type="source" position={Position.Bottom} className={handleCls} />
    </div>
  )
}
