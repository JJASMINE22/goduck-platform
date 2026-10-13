/* 拓扑预览节点卡 —— 只读精简版编辑器节点卡（type='topo'）。
    角色色 token：main=紫 / sub=青 / ghost=灰；「低饱和 tint 填充 + 高饱和描边」
    由 CSS 以 --topo-tone 派生（color-mix），深浅主题零硬编码。
    交互经 TopologyInteractionContext：悬停 intent-trace（压暗非邻居）、点击聚焦链路。
    手柄仅作边端点锚（隐藏不可交互）——预览画布禁连线。 */
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import { Bot, Boxes, Ghost } from 'lucide-react'
import { useContext } from 'react'
import type { ReactNode } from 'react'
import type { TopologyNode } from '@/types'
import { TopologyInteractionContext } from './interaction'

export interface TopologyNodeCardData {
  node: TopologyNode
  [key: string]: unknown
}
export type TopoFlowNode = Node<TopologyNodeCardData, 'topo'>

const TONE_BY_ROLE = {
  main: 'var(--color-role-agent-main)',
  sub: 'var(--color-role-agent-sub)',
  ghost: 'var(--color-text-tertiary)',
} as const

const ICON_BY_ROLE = {
  main: Bot,
  sub: Boxes,
  ghost: Ghost,
} as const

function titleOf(node: TopologyNode): string {
  if (node.role === 'main') return '主代理'
  return node.name || '未命名子代理'
}

export function TopologyNodeCard({ id, data }: NodeProps<TopoFlowNode>): ReactNode {
  const ctx = useContext(TopologyInteractionContext)
  const node = data.node
  const lit = ctx?.litIds ?? null
  const dimmed = !!lit && !lit.has(id)
  const active = ctx?.hoverId === id || ctx?.focusId === id
  const cls =
    `topo-node topo-node--${node.role}` +
    (dimmed ? ' topo-node--dim' : '') +
    (active ? ' topo-node--active' : '')
  const Icon = ICON_BY_ROLE[node.role]
  // 摘要缺省：主代理无 prompt 首行时给中性占位，子代理缺 description 同理
  const summary = node.summary || (node.role === 'main' ? '主代理网关' : '无描述')
  const toolCount = node.caps?.tools?.length ?? 0
  const skillCount = node.caps?.skills?.length ?? 0

  return (
    <div
      className={cls}
      style={
        {
          '--topo-tone': TONE_BY_ROLE[node.role],
          '--topo-depth': node.depth,
        } as React.CSSProperties
      }
      onMouseEnter={() => ctx?.onHover(id)}
      onMouseLeave={() => ctx?.onHover(null)}
      onClick={(e) => {
        e.stopPropagation()
        ctx?.onToggleFocus(id)
      }}
      aria-label="点击聚焦该节点的委派链路"
    >
      <Handle type="target" position={Position.Top} className="topo-handle" isConnectable={false} />
      <div className="topo-node__head">
        <span className="topo-node__dot" />
        <Icon size={13} className="topo-node__icon" />
        <span className="topo-node__title">{titleOf(node)}</span>
        {node.is_dicted ? <span className="topo-node__badge">dict</span> : null}
        {node.dynamic_dispatch ? <span className="topo-node__badge">dyn</span> : null}
      </div>
      <div className="topo-node__body">
        <p className="topo-node__summary">{summary}</p>
        {node.role === 'ghost' ? null : (
          <div className="topo-node__meta">
            <span title={`${toolCount} 个工具`}>tools {toolCount}</span>
            <span aria-hidden="true">·</span>
            <span title={`${skillCount} 个技能`}>skills {skillCount}</span>
          </div>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} className="topo-handle" isConnectable={false} />
    </div>
  )
}
