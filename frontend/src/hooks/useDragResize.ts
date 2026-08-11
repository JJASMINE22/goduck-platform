/* 抽屉/面板拖拽调宽 —— 右侧抽屉的左边缘可拖拽改变宽度（无第三方依赖）。
   - edge='left'（右侧抽屉，默认）：向左拖变宽，delta = startX - clientX。
   - 拖拽期间禁用文本选中、设全局 col-resize 光标，松开恢复。
   - clamp 到 [min, max]，避免拖出可视区；max 默认留 24px 边距防溢出屏幕。
   - 可选 storageKey：宽度持久化到 localStorage，跨刷新保留用户偏好。 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'

interface UseDragResizeOptions {
  initial?: number
  min?: number
  max?: number
  /** 拖拽边：'left'=右侧抽屉（左边缘拖拽，默认）；'right'=左侧抽屉 */
  edge?: 'left' | 'right'
  /** 持久化键；提供则宽度写入 localStorage 跨刷新保留 */
  storageKey?: string
}

function clamp(v: number, min: number, max: number): number {
  return Math.round(Math.min(max, Math.max(min, v)))
}

export function useDragResize(opts: UseDragResizeOptions = {}) {
  const { initial = 960, min = 400, max = 1600, edge = 'left', storageKey } = opts

  const [width, setWidth] = useState<number>(() => {
    if (storageKey) {
      const saved = Number.parseInt(localStorage.getItem(storageKey) ?? '', 10)
      if (!Number.isNaN(saved)) return clamp(saved, min, max)
    }
    return initial
  })

  const drag = useRef<{ startX: number; startWidth: number } | null>(null)

  const apply = useCallback(
    (w: number) => {
      const clamped = clamp(w, min, max)
      setWidth(clamped)
      if (storageKey) {
        try {
          localStorage.setItem(storageKey, String(clamped))
        } catch {
          // 隐私模式等写入失败：忽略，宽度仍生效（仅本次会话）
        }
      }
    },
    [min, max, storageKey],
  )

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!drag.current) return
      const delta = edge === 'left' ? drag.current.startX - e.clientX : e.clientX - drag.current.startX
      apply(drag.current.startWidth + delta)
    }
    const onUp = () => {
      if (!drag.current) return
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

  /** 绑定到拖拽手柄的 onMouseDown。捕获起始坐标与当前宽度。 */
  const onResizeStart = useCallback(
    (e: ReactMouseEvent) => {
      e.preventDefault()
      drag.current = { startX: e.clientX, startWidth: width }
      document.body.style.userSelect = 'none'
      document.body.style.cursor = 'col-resize'
    },
    [width],
  )

  /** 重置为初始宽度（建议绑到手柄 onDoubleClick）。 */
  const reset = useCallback(() => apply(initial), [apply, initial])

  return { width, onResizeStart, reset }
}
