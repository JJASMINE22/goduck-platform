/* 拓扑预览画布 —— React Flow 只读模式（禁拖拽/连线/滚轮缩放，保留平移与捏合）。
    preventScrolling=false：滚轮冒泡给 Popover 滚动，不与页面抢占。
    交互状态由父组件持有（详情条/图例共享），经 Context 注入节点卡。 */
import '@xyflow/react/dist/style.css'
import { Background, ReactFlow, type NodeTypes } from '@xyflow/react'
import { useMemo, type ReactNode } from 'react'
import type { TopologyGraph } from '@/types'
import { indexGraph, toFlowEdges, toFlowNodes } from './graph'
import { TopologyInteractionContext, type TopologyInteraction } from './interaction'
import { TopologyNodeCard } from './TopologyNodeCard'

const NODE_TYPES: NodeTypes = { topo: TopologyNodeCard }

export function TopologyMiniCanvas({
  graph,
  interaction,
}: {
  graph: TopologyGraph
  interaction: TopologyInteraction
}): ReactNode {
  const index = useMemo(() => indexGraph(graph), [graph])
  // 节点仅依赖 graph：悬停/聚焦时引用稳定（RF 不全量重渲，防悬浮抖动）；
  // 视觉态（dim/active）经 Context 下发到节点卡。边随高亮集重算（trace/dim 类名）。
  const rfNodes = useMemo(() => toFlowNodes(graph), [graph])
  const rfEdges = useMemo(() => toFlowEdges(graph, index, interaction.litIds), [graph, index, interaction.litIds])

  return (
    <TopologyInteractionContext.Provider value={interaction}>
      <div className="topo-canvas">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={NODE_TYPES}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          elementsSelectable
          panOnDrag
          panOnScroll={false}
          zoomOnScroll={false}
          zoomOnPinch
          preventScrolling={false}
          minZoom={0.2}
          maxZoom={1.25}
          fitView
          fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
          proOptions={{ hideAttribution: true }}
          onPaneClick={interaction.onClearFocus}
        >
          <Background gap={16} size={1} color="var(--color-border-soft)" />
        </ReactFlow>
      </div>
    </TopologyInteractionContext.Provider>
  )
}
