/* 图表横向拖拽平移（走查调整：气泡收窄后宽图表以自然宽度横向滚动）。
    mermaid/SVG 图表卡正文容器挂 panHandlers 即获得"抓手拖动"能力：
    - 仅当容器实际溢出（scrollWidth > clientWidth）时按下才可能接管；
    - 横向主导位移超过 6px 才进入平移（纵向滚动/点击放大/未拖动不干扰）；
    - 平移期间挂 .is-panning（grabbing 光标 + 禁选区，见 index.css），松手解除；
    - 配合容器 touch-action: pan-y：触摸设备纵向滚动手势照常原生滚动。
    状态为模块级单例：同一时刻只有一个指针，跨实例共享安全。 */

export interface PanState {
  down: boolean
  panning: boolean
  x: number
  y: number
  left: number
  id: number
}

export const panState: PanState = { down: false, panning: false, x: 0, y: 0, left: 0, id: -1 }

const THRESHOLD = 6

export const panHandlers = {
  onPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (e.button !== 0) return
    const el = e.currentTarget
    // 未溢出：无平移可言，放行原生行为（点击放大等）
    if (el.scrollWidth <= el.clientWidth) return
    panState.down = true
    panState.panning = false
    panState.x = e.clientX
    panState.y = e.clientY
    panState.left = el.scrollLeft
    panState.id = e.pointerId
    el.setPointerCapture(e.pointerId)
  },
  onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    if (!panState.down || e.pointerId !== panState.id) return
    const el = e.currentTarget
    const dx = e.clientX - panState.x
    if (!panState.panning) {
      // 未达阈值，或纵向位移占主导（用户在垂直滚动）时不接管
      if (Math.abs(dx) < THRESHOLD || Math.abs(e.clientY - panState.y) > Math.abs(dx)) return
      panState.panning = true
      el.classList.add('is-panning')
    }
    el.scrollLeft = panState.left - dx
  },
  onPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    panState.down = false
    e.currentTarget.classList.remove('is-panning')
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
  },
  onPointerCancel(e: React.PointerEvent<HTMLDivElement>) {
    panState.down = false
    e.currentTarget.classList.remove('is-panning')
  },
} satisfies Record<string, React.PointerEventHandler<HTMLDivElement>>
