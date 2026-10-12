/* 面板拖拽调宽/调高 —— 无第三方依赖，四方向语义统一（替代各页面内联拖拽逻辑）。
   edge 指拖拽手柄所在的边（即面板被拖动的那条边），决定方向与光标：
   - 'left'  ：右侧面板的左缘手柄，向左拖变宽（delta = startX - clientX）
   - 'right' ：左侧面板的右缘手柄，向右拖变宽（delta = clientX - startX）
   - 'top'   ：底部面板的上缘手柄，向上拖变高（delta = startY - clientY）
   - 'bottom'：顶部面板的下缘手柄，向下拖变高（delta = clientY - startY）
   拖拽期间禁用文本选中、设全局 resize 光标，并在手柄上标记 data-resizing
   （CSS 据此在拖拽全程高亮，不依赖鼠标仍悬停在手柄上）。
   尺寸 clamp 到 [min, max]，避免拖出可视区；可选 storageKey 持久化到
   localStorage，跨刷新保留用户偏好。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'

export type ResizeEdge = 'left' | 'right' | 'top' | 'bottom'

interface UseDragResizeOptions {
  initial?: number
  min?: number
  max?: number
  /** 拖拽手柄所在边；默认 'left'（右侧面板） */
  edge?: ResizeEdge
  /** 持久化键；提供则尺寸写入 localStorage 跨刷新保留 */
  storageKey?: string
}

const VERTICAL_EDGES: ReadonlyArray<ResizeEdge> = ['top', 'bottom']

function clamp(v: number, min: number, max: number): number {
  return Math.round(Math.min(max, Math.max(min, v)))
}

/** 手柄方向语义：向"远离面板锚定边"的方向拖动 → 尺寸增大 */
function edgeDelta(edge: ResizeEdge, startX: number, startY: number, ev: MouseEvent): number {
  const dx = ev.clientX - startX
  const dy = ev.clientY - startY
  switch (edge) {
    case 'left':
      return -dx
    case 'right':
      return dx
    case 'top':
      return -dy
    case 'bottom':
      return dy
  }
}

export function useDragResize(opts: UseDragResizeOptions = {}) {
  const { initial = 480, min = 200, max = 1600, edge = 'left', storageKey } = opts

  const [size, setSize] = useState<number>(() => {
    if (storageKey) {
      const saved = Number.parseInt(localStorage.getItem(storageKey) ?? '', 10)
      if (!Number.isNaN(saved)) return clamp(saved, min, max)
    }
    return initial
  })

  const drag = useRef<{ startX: number; startY: number; startSize: number; el: HTMLElement } | null>(null)

  const apply = useCallback(
    (v: number) => {
      const clamped = clamp(v, min, max)
      setSize(clamped)
      if (storageKey) {
        try {
          localStorage.setItem(storageKey, String(clamped))
        } catch {
          // 隐私模式等写入失败：忽略，尺寸仍生效（仅本次会话）
        }
      }
    },
    [min, max, storageKey],
  )

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!drag.current) return
      apply(drag.current.startSize + edgeDelta(edge, drag.current.startX, drag.current.startY, e))
    }
    const onUp = () => {
      if (!drag.current) return
      // 元素可能在拖拽中被卸载：对已脱离文档的节点移除属性无害
      drag.current.el.removeAttribute('data-resizing')
      drag.current = null
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [edge, apply])

  /** 绑定到拖拽手柄的 onMouseDown。捕获起始坐标、当前尺寸与手柄元素。 */
  const onResizeStart = useCallback(
    (e: ReactMouseEvent) => {
      e.preventDefault()
      const el = e.currentTarget as HTMLElement
      drag.current = { startX: e.clientX, startY: e.clientY, startSize: size, el }
      el.setAttribute('data-resizing', '')
      document.body.style.userSelect = 'none'
      document.body.style.cursor = VERTICAL_EDGES.includes(edge) ? 'row-resize' : 'col-resize'
    },
    [size, edge],
  )

  /** 重置为初始尺寸（建议绑到手柄 onDoubleClick）。 */
  const reset = useCallback(() => apply(initial), [apply, initial])

  /** 暴露钳制写入：供外部在上限变化（如容器变窄）时同步收敛已存尺寸。 */
  return { size, onResizeStart, reset, apply }
}
