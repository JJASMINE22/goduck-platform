/* 可拖拉调高的多行输入 —— 取代原生 textarea 右下角斜线缩放手柄（OS 原生观感
   与项目流体玻璃设计不符，且热区仅 10px 难命中）。
   方案：底部通栏抓手条（居中胶囊视觉，默认低调半隐，悬停/拖拽点亮主色并展宽，
   与会话 composer 的 row-resizer 同款交互语义），pointer 事件驱动改高；
   axis="xy"（顶栏描述等窄框场景）额外支持横向拖拉调宽。
   高/宽钳制：minH/maxH（默认上限 65vh 防拖溢视口）、minW/maxW。
   其余 props 原样透传 antd Input.TextArea（value/onChange/rows/variant 等）。 */
import { Input } from 'antd'
import { type ComponentProps, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode, useState } from 'react'

const clamp = (min: number, max: number, v: number) => Math.min(max, Math.max(min, v))

type TextAreaProps = ComponentProps<typeof Input.TextArea>

export function DragTextarea({
  axis = 'y',
  minH = 32,
  maxH,
  minW = 160,
  maxW = 420,
  wrapStyle,
  ...rest
}: TextAreaProps & {
  /** 拖拉轴：y=仅高度（默认），xy=宽×高（窄框场景双向） */
  axis?: 'y' | 'xy'
  /** 高度钳制（px）；maxH 缺省取视口 65vh */
  minH?: number
  maxH?: number
  /** 宽度钳制（px），仅 axis="xy" 生效 */
  minW?: number
  maxW?: number
  /** 包装层（textarea + 抓手条）样式，如外距/定宽 */
  wrapStyle?: CSSProperties
}): ReactNode {
  // 拖拉结果（null = 未拖过，行高由 rows 决定）
  const [size, setSize] = useState<{ h: number; w?: number } | null>(null)

  const onGrabDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    const ta = e.currentTarget.parentElement?.querySelector('textarea')
    const startH = ta?.offsetHeight ?? minH
    const startW = ta?.offsetWidth ?? minW
    const startY = e.clientY
    const startX = e.clientX
    const hMax = maxH ?? Math.max(minH, window.innerHeight * 0.65)
    const grip = e.currentTarget
    const onMove = (ev: PointerEvent) => {
      // 手柄在盒子的下缘（非会话页 dock 的上缘手柄）：向下/向右拉 = 变高/变宽，
      // 与 ns-resize / nwse-resize 光标语义一致
      const h = clamp(minH, hMax, startH + (ev.clientY - startY))
      setSize(
        axis === 'xy'
          ? { h, w: clamp(minW, maxW, startW + (ev.clientX - startX)) }
          : { h },
      )
    }
    const onUp = () => {
      grip.removeAttribute('data-resizing')
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
      document.removeEventListener('pointermove', onMove)
      document.removeEventListener('pointerup', onUp)
    }
    grip.setAttribute('data-resizing', '')
    document.body.style.userSelect = 'none'
    document.body.style.cursor = axis === 'xy' ? 'nwse-resize' : 'ns-resize'
    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
  }

  return (
    <div className="drag-ta-wrap" style={wrapStyle}>
      <Input.TextArea
        {...rest}
        className={rest.className ? `drag-ta ${rest.className}` : 'drag-ta'}
        style={{ ...rest.style, resize: 'none', height: size?.h, width: size?.w } as CSSProperties}
      />
      <div
        className={axis === 'xy' ? 'drag-ta__grip drag-ta__grip--xy' : 'drag-ta__grip'}
        onPointerDown={onGrabDown}
        title={axis === 'xy' ? '拖拉调整宽高' : '拖拉调整高度'}
      >
        <span className="drag-ta__grip-bar" />
      </div>
    </div>
  )
}
