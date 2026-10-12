/* 智能体画布 —— xyflow 渲染归一化节点树。
   结构（节点/父子）真源 = store.nodes；几何（坐标）真源 = positions(state) + dagre 布局。
   - rfNodes/rfEdges 由 useMemo 从 store + positions 派生；缺失坐标用 dagre 整树布局补齐
   - 拖拽节点 → onNodesChange 写回 positions 触发重渲染
   - 连线（onConnect）= 换父级（target 的父改为 source）。拖拽进行中按 canReparent 同款
     规则高亮合法/非法落点并在起点浮出语义提示；isValidConnection 兜底拦截非法吸附
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
  type HandleType,
  type Node,
  type NodeChange,
  type NodeTypes,
  type OnConnectStartParams,
} from '@xyflow/react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { findTemplate } from '../templates'
import { canReparent, descendantIds, type AgentGraphNode } from '../graphModel'
import { layoutTree, type XYPos } from './layout'
import { AgentNodeCard, type ConnectRole } from './AgentNode'

const EDGE_TYPE = 'smoothstep'

/** 鼠标距节点手柄该半径内释放即吸附成线（配合手柄透明热区，降低瞄准精度要求） */
const CONNECTION_RADIUS = 30

/** 起点提示文案按起始手柄方向区分语义（source=起点当新父 / target=起点当新子） */
const HINT_TAKE_CHILD = '拖到目标节点上，将其收为子代理'
const HINT_AS_CHILD = '拖到目标节点上，成为其子代理'

/** 连线起点信息 */
interface ConnectFrom {
  nodeId: string
  handleType: HandleType | null
}

/** 连线进行中为每个节点计算落点角色。
 *  判定与 store.reparent 的守卫（canReparent）完全一致，保证高亮提示与松手结果不分歧。 */
function computeConnectRoles(
  nodes: AgentGraphNode[],
  from: ConnectFrom,
): Map<string, ConnectRole> {
  const roles = new Map<string, ConnectRole>()
  for (const n of nodes) {
    if (n.id === from.nodeId) {
      roles.set(n.id, 'self')
      continue
    }
    // 连线语义 = reparent(target, source)：起始手柄类型决定起点在潜在连线中扮演父还是子
    const childId = from.handleType === 'target' ? from.nodeId : n.id
    const parentId = from.handleType === 'target' ? n.id : from.nodeId
    roles.set(n.id, canReparent(nodes, childId, parentId) ? 'valid' : 'invalid')
  }
  return roles
}

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
  // 连线拖拽进行中的起点（null = 未连线）
  const [connectFrom, setConnectFrom] = useState<ConnectFrom | null>(null)

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
    // 连线进行中才计算落点角色；起点已不在树中（如草稿被外部重置）则按未连线处理
    const roles =
      connectFrom && nodes.some((n) => n.id === connectFrom.nodeId)
        ? computeConnectRoles(nodes, connectFrom)
        : null
    const connectHint =
      connectFrom?.handleType === 'target' ? HINT_AS_CHILD : HINT_TAKE_CHILD
    return {
      rfNodes: nodes.map((n) => ({
        id: n.id,
        type: 'agent',
        position: resolved[n.id] ?? { x: 0, y: 0 },
        data: {
          node: n,
          selected: n.id === selectedNodeId,
          dynamic: dynamicIds.has(n.id),
          connecting: !!roles,
          connectRole: roles?.get(n.id) ?? 'none',
          connectHint: roles?.get(n.id) === 'self' ? connectHint : undefined,
        },
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
  }, [nodes, selectedNodeId, positions, connectFrom])

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

  const onConnectStart = useCallback(
    (_e: MouseEvent | TouchEvent, { nodeId, handleType }: OnConnectStartParams) => {
      if (nodeId) setConnectFrom({ nodeId, handleType })
    },
    [],
  )

  // 无论成线、取消还是释放在空白处，都结束高亮态
  const onConnectEnd = useCallback(() => setConnectFrom(null), [])

  // 吸附兜底：仅允许与 store.reparent 守卫一致的连线成线
  const isValidConnection = useCallback(
    (conn: Edge | Connection) =>
      !!conn.source && !!conn.target && canReparent(nodes ?? [], conn.target, conn.source),
    [nodes],
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
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        isValidConnection={isValidConnection}
        connectionRadius={CONNECTION_RADIUS}
        deleteKeyCode={null}
        fitView
        fitViewOptions={{ maxZoom: 1, padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
      >
        <Background gap={18} size={1} color="var(--color-border-soft)" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeStrokeWidth={2}
          nodeColor="var(--color-role-agent-sub)"
          maskColor="var(--color-bg)"
          style={{ background: 'var(--color-bg-elevated)' }}
        />
      </ReactFlow>
    </div>
  )
}
