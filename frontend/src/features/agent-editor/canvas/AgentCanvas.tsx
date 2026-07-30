/* 智能体画布 —— xyflow 渲染归一化节点树。
   结构（节点/父子）真源 = store.nodes；几何（坐标）真源 = positions(state) + dagre 布局。
   - rfNodes/rfEdges 由 useMemo 从 store + positions 派生；缺失坐标用 dagre 整树布局补齐
   - 拖拽节点 → onNodesChange 写回 positions 触发重渲染
   - 连线（onConnect）= 换父级（target 的父改为 source）
   - 模板从侧栏拖入（onDrop）= 建子节点并定位到落点
   - 删除走节点卡按钮（store.removeNode），禁用键盘删除以免与 store 不同步 */
import '@xyflow/react/dist/style.css'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
  type NodeTypes,
} from '@xyflow/react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { findTemplate } from '../templates'
import { descendantIds } from '../graphModel'
import { layoutTree, type XYPos } from './layout'
import { AgentNodeCard } from './AgentNode'

const EDGE_TYPE = 'smoothstep'

export function AgentCanvas(): ReactNode {
  return (
    <ReactFlowProvider>
      <CanvasInner />
    </ReactFlowProvider>
  )
}

function CanvasInner(): ReactNode {
  const nodes = useAgentEditorStore((s) => s.nodes)
  const selectedNodeId = useAgentEditorStore((s) => s.selectedNodeId)
  const selectNode = useAgentEditorStore((s) => s.selectNode)
  const reparent = useAgentEditorStore((s) => s.reparent)
  const addNode = useAgentEditorStore((s) => s.addNode)

  const { screenToFlowPosition } = useReactFlow()
  // 坐标真源：手动拖拽/落点写入；dagre 仅补齐尚未定位的节点
  const [positions, setPositions] = useState<Record<string, XYPos>>({})

  const rootId = nodes?.find((n) => n.parentId === null)?.id ?? null

  // 派生画布节点/边：dagre 布局打底，positions 覆盖手动定位
  const { rfNodes, rfEdges } = useMemo(() => {
    if (!nodes) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const laid = layoutTree(nodes)
    const resolved = { ...laid, ...positions }
    // 收集所有 dynamic_dispatch 子树(含自身)的节点 id: 这些节点显示动态光环
    const dynamicIds = new Set<string>()
    for (const n of nodes) {
      if (n.dynamic_dispatch) {
        for (const id of descendantIds(nodes, n.id)) dynamicIds.add(id)
      }
    }
    return {
      rfNodes: nodes.map((n) => ({
        id: n.id,
        type: 'agent',
        position: resolved[n.id] ?? { x: 0, y: 0 },
        data: { node: n, selected: n.id === selectedNodeId, dynamic: dynamicIds.has(n.id) },
      })),
      rfEdges: nodes
        .filter((n) => n.parentId)
        .map((n) => ({
          id: `e-${n.parentId}-${n.id}`,
          source: n.parentId as string,
          target: n.id,
          type: EDGE_TYPE,
          animated: true,
        })),
    }
  }, [nodes, selectedNodeId, positions])

  // 拖拽：把最新坐标写回 positions
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setPositions((prev) => {
      let next = prev
      for (const c of changes) {
        if (c.type === 'position' && c.position) {
          if (next === prev) next = { ...prev }
          next[c.id] = c.position
        }
      }
      return next
    })
  }, [])

  const onConnect = useCallback(
    (conn: Connection) => {
      if (conn.source && conn.target && conn.source !== conn.target) {
        reparent(conn.target, conn.source) // target 的父级改为 source
      }
    },
    [reparent],
  )

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }, [])

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault()
      const key = e.dataTransfer.getData('application/subagent-template')
      const tpl = key ? findTemplate(key) : undefined
      const parentId = selectedNodeId ?? rootId
      if (!parentId) return
      const pos = screenToFlowPosition({ x: e.clientX, y: e.clientY })
      const newId = addNode(parentId, tpl?.fields)
      if (newId) setPositions((p) => ({ ...p, [newId]: pos }))
    },
    [selectedNodeId, rootId, addNode, screenToFlowPosition],
  )

  return (
    <div className="agent-canvas" onDrop={onDrop} onDragOver={onDragOver}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={{ agent: AgentNodeCard } as NodeTypes}
        onNodesChange={onNodesChange}
        onNodeClick={(_, n) => selectNode(n.id)}
        onConnect={onConnect}
        deleteKeyCode={null}
        fitView
        fitViewOptions={{ maxZoom: 1, padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={18} size={1} color="rgba(60, 64, 67, 0.12)" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeStrokeWidth={2}
          nodeColor="var(--color-role-agent-sub)"
          maskColor="rgba(60, 64, 67, 0.08)"
          style={{ background: 'var(--color-bg-elevated)' }}
        />
      </ReactFlow>
    </div>
  )
}
