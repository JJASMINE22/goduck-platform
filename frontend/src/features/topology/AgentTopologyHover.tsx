/* 智能体拓扑档案卡（DESIGN §10.2）—— 列表行名称上悬浮，按需拉取 topology.json。
    仅在 Popover 打开时启用查询（enabled），避免列表渲染即 N+1；同一 agent 缓存复用。
    四段式（archify「证据控制台」语言 × goduck 玻璃）：
    头带（名称/模型/用途 + 画布编辑入口）→ 只读拓扑画布（悬停 intent-trace /
    点击聚焦链路）→ 图例（角色 tone 点 + 计数）→ 焦点详情条（能力渐进披露）。 */
import { Popover, Spin, Tag } from 'antd'
import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Pencil } from 'lucide-react'
import { useAgentTopology } from '@/hooks/useAgents'
import type { TopologyGraph, TopologyNode, UUID } from '@/types'
import { focusSet, graphRows, hoverSet, indexGraph } from './graph'
import type { TopologyInteraction } from './interaction'
import { TopologyMiniCanvas } from './TopologyMiniCanvas'

/** 悬停提交去抖：进入 40ms（近乎即时）、离开 110ms（平滑跨过节点间隙）。
    未去抖时鼠标在卡片与间隙间微移会快速切换全局 dim/亮起——闪烁主因之一。 */
const HOVER_COMMIT_MS = 40
const HOVER_CLEAR_MS = 110

/** 画布高度按拓扑行数自适应：单智能体不再撑满 300px 留大片空白。 */
function canvasHeight(rows: number): number {
  return Math.min(300, Math.max(160, 60 + rows * 110))
}

export function AgentTopologyHover({ id, children }: { id: UUID; children: ReactNode }): ReactNode {
  const [open, setOpen] = useState(false)
  const { data, isLoading, isError } = useAgentTopology(id, open)
  const navigate = useNavigate()

  // 交互态与 Popover 同生命周期：关闭即复位（destroyTooltipOnHide 卸载画布，入场动画下次重播）
  const [hoverId, setHoverId] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const hoverTimer = useRef<number | null>(null)

  const commitHover = useCallback((nid: string | null) => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => setHoverId(nid), nid ? HOVER_COMMIT_MS : HOVER_CLEAR_MS)
  }, [])
  const clearInteraction = useCallback(() => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = null
    }
    setHoverId(null)
    setFocusId(null)
  }, [])

  const index = useMemo(() => (data ? indexGraph(data) : null), [data])
  // 防御：交互 id 不在当前图中（数据已刷新/切换）时视为无交互态
  const safeHover = hoverId && index?.byId.has(hoverId) ? hoverId : null
  const safeFocus = focusId && index?.byId.has(focusId) ? focusId : null
  const litIds = useMemo(() => {
    if (!index) return null
    if (safeFocus) return focusSet(index, safeFocus)
    if (safeHover) return hoverSet(index, safeHover)
    return null
  }, [index, safeFocus, safeHover])

  const interaction: TopologyInteraction = useMemo(
    () => ({
      litIds,
      hoverId: safeHover,
      focusId: safeFocus,
      onHover: commitHover,
      onToggleFocus: (nid: string) => setFocusId((cur) => (cur === nid ? null : nid)),
      onClearFocus: clearInteraction,
    }),
    [litIds, safeHover, safeFocus, commitHover, clearInteraction],
  )

  // 详情条跟随悬停优先、聚焦兜底（悬停离开聚焦节点时详情不闪空）
  const detailId = safeHover ?? safeFocus
  const detail = detailId ? (index?.byId.get(detailId) ?? null) : null

  return (
    <Popover
      trigger="hover"
      placement="rightTop"
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        if (!o) clearInteraction()
      }}
      mouseEnterDelay={0.25}
      mouseLeaveDelay={0.15}
      destroyTooltipOnHide
      classNames={{ root: 'topo-popover-root', body: 'topo-popover' }}
      content={
        <TopologyCard
          graph={data ?? null}
          loading={isLoading}
          error={isError}
          interaction={interaction}
          detail={detail}
          onEdit={() => navigate(`/agents/${id}`)}
        />
      }
    >
      {children}
    </Popover>
  )
}

/** 档案卡主体：loading / error / 就绪三态。尺寸与玻璃底由 CSS（.topo-card）定档。 */
function TopologyCard({
  graph,
  loading,
  error,
  interaction,
  detail,
  onEdit,
}: {
  graph: TopologyGraph | null
  loading: boolean
  error: boolean
  interaction: TopologyInteraction
  detail: TopologyNode | null
  onEdit: () => void
}): ReactNode {
  if (error) {
    return (
      <div className="topo-card">
        <div className="topo-card__state topo-card__state--error">加载失败，请重试</div>
      </div>
    )
  }
  if (loading || !graph) {
    return (
      <div className="topo-card">
        <div className="topo-card__state">
          <Spin size="small" />
        </div>
      </div>
    )
  }
  // 单节点图收窄卡片：横向不留大片空白；行数少时压低画布高度
  const rows = graphRows(graph)
  const compact = graph.nodes.length <= 1
  return (
    <div className="topo-card" style={compact ? { width: 360 } : undefined}>
      <header className="topo-card__head">
        <span className="topo-card__name" title={graph.agent_name}>
          {graph.agent_name}
        </span>
        {graph.usage === 'cron' ? <Tag color="geekblue">定时任务</Tag> : null}
        <span className="topo-card__model" title={graph.model_name ?? undefined}>
          {graph.model_name || '未绑定模型'}
        </span>
        <button type="button" className="topo-card__edit" onClick={onEdit}>
          <Pencil size={12} />
          画布编辑
        </button>
      </header>
      <div className="topo-card__canvas" style={{ height: canvasHeight(rows) }}>
        <TopologyMiniCanvas graph={graph} interaction={interaction} />
      </div>
      <div className="topo-card__legend">
        <span className="topo-legend__item">
          <i
            className="topo-legend__dot"
            style={{ '--tone': 'var(--color-role-agent-main)' } as React.CSSProperties}
          />
          主代理
        </span>
        <span className="topo-legend__item">
          <i
            className="topo-legend__dot"
            style={{ '--tone': 'var(--color-role-agent-sub)' } as React.CSSProperties}
          />
          子代理
        </span>
        <span className="topo-legend__meta">
          {graph.nodes.length} 节点 · {graph.edges.length} 委派
        </span>
      </div>
      <footer className="topo-card__detail">
        {detail ? <NodeDetail node={detail} /> : <span className="topo-detail__hint">悬停节点查看能力 · 点击聚焦委派链路</span>}
      </footer>
    </div>
  )
}

/** 节点能力详情（Semantic Passport 式渐进披露）：名称 + 能力 chip 组。 */
function NodeDetail({ node }: { node: TopologyNode }): ReactNode {
  const caps = node.caps
  const tools = caps?.tools ?? []
  const skills = caps?.skills ?? []
  const hitl = caps?.hitl ?? []
  const title = node.role === 'main' ? '主代理' : node.name || '未命名子代理'
  const boolCaps = [
    caps?.memory && '记忆',
    caps?.workspace && '工作区',
    caps?.shell && 'Shell',
    caps?.rubric && 'Rubric',
    caps?.task_planning && '规划',
    node.is_dicted && 'dicted',
  ].filter((v): v is string => !!v)
  const hasAny = tools.length || skills.length || hitl.length || boolCaps.length
  return (
    <>
      <span className="topo-detail__name">{title}</span>
      {hasAny ? (
        <>
          {tools.length ? <span className="topo-chip">tools: {joinCapped(tools)}</span> : null}
          {skills.length ? <span className="topo-chip">skills: {joinCapped(skills)}</span> : null}
          {hitl.length ? <span className="topo-chip topo-chip--on">HITL: {joinCapped(hitl)}</span> : null}
          {boolCaps.map((c) => (
            <span key={c} className="topo-chip">
              {c}
            </span>
          ))}
        </>
      ) : (
        <span className="topo-detail__hint">无附加能力</span>
      )}
    </>
  )
}

/** 列表拼串，超过 4 项截断追加 (+N)（与后端 mermaid _cap_join 同策略）。 */
function joinCapped(items: string[], max = 4): string {
  if (items.length <= max) return items.join(', ')
  return `${items.slice(0, max).join(', ')} (+${items.length - max})`
}
