/* 画布链路高亮契约 —— CanvasInner 持有 hover 状态与链路集，节点卡经 Context 消费。
   Context 下发使 hover 变化只重渲节点卡本身，不触发 rfNodes 重建（防抖动/防全量重渲，
   与拓扑预览 features/topology/interaction.ts 同构）。 */
import { createContext } from 'react'

export interface CanvasInteraction {
  /** 悬浮链路集（null = 无悬停或连线进行中，全部常态显示） */
  litIds: ReadonlySet<string> | null
  /** 悬停中的节点 id（null = 无） */
  hoverId: string | null
  onHover: (id: string | null) => void
}

export const CanvasInteractionContext = createContext<CanvasInteraction | null>(null)
