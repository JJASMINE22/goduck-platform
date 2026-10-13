/* 拓扑预览交互契约 —— 状态由父组件（AgentTopologyHover）持有并下发音调，
   画布内自定义节点卡经 Context 消费（React context 穿透 RF 的节点 memo，
   悬停/聚焦变化无需重建节点数据）。详情条/图例在父层直接读同一状态。 */
import { createContext } from 'react'

export interface TopologyInteraction {
  /** 当前应保持点亮的节点集（null = 无悬停/聚焦，全部常态显示） */
  litIds: ReadonlySet<string> | null
  /** 悬停中的节点 id（null = 无） */
  hoverId: string | null
  /** 聚焦锁定的节点 id（null = 无；点击节点切换、点击画布空白清除） */
  focusId: string | null
  onHover: (id: string | null) => void
  onToggleFocus: (id: string) => void
  onClearFocus: () => void
}

export const TopologyInteractionContext = createContext<TopologyInteraction | null>(null)
