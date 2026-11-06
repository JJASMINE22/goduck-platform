/* ZoomStage —— 图表/图像中央放大查看层（mermaid / SVG 围栏 / 普通图像共用）。
    全屏遮罩 + 内容居中适配打开（替代旧右侧 Drawer「伪放大」——抽屉宽度反而
    比气泡更窄）；能力对齐主流图片查看器：
    - 滚轮缩放：以光标为锚（光标下的内容点缩放前后不动）
    - 工具条 ±/适配百分比、双击复位、拖拽平移（pointer capture）
    - Esc / 点击遮罩关闭；打开期间锁定页面滚动
    内容不复制渲染状态：SVG 用净化后的源码直注，mermaid 打开时快照已渲
    innerHTML（含内联样式，主题色已烘焙），img 用原 src。 */
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Maximize2, X, ZoomIn, ZoomOut } from 'lucide-react'

const SCALE_MIN = 0.05
const SCALE_MAX = 8
/** 每次滚轮/按钮的缩放步进倍率 */
const ZOOM_STEP = 1.2
/** 适配视图四周留白（遮罩边缘 + 底部工具条高度） */
const FIT_MARGIN = 32
const FIT_MARGIN_BOTTOM = 96

interface View {
  scale: number
  x: number
  y: number
}

export function ZoomStage({
  open,
  onClose,
  /** 块状图表（svg/mermaid）：内容基准宽取 min(92vw, 1600px) 按纵横比定高；
      图像则取自然尺寸（max 92vw/78vh） */
  block = false,
  children,
}: {
  open: boolean
  onClose: () => void
  block?: boolean
  children: ReactNode
}): ReactNode {
  const stageRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  // view 就绪前内容不可见（避免未缩放的大图在左上角闪一帧）
  const [view, setView] = useState<View | null>(null)
  // 适配缩放（百分比的分母）；与 view 同源更新，非渲染期读 ref 的等价信息放 state
  const [fitScale, setFitScale] = useState(1)
  const dragRef = useRef({ down: false, moved: false, x: 0, y: 0, vx: 0, vy: 0 })

  /** 适配视图：内容完整可见（等比缩放 + 居中）。窗口尺寸/内容尺寸变化时复适配。 */
  const fit = useCallback(() => {
    const stage = stageRef.current
    const content = contentRef.current
    if (!stage || !content) return
    const w = content.offsetWidth
    const h = content.offsetHeight
    if (!w || !h) return
    const availW = Math.max(60, stage.clientWidth - FIT_MARGIN * 2)
    const availH = Math.max(60, stage.clientHeight - FIT_MARGIN - FIT_MARGIN_BOTTOM)
    const scale = Math.min(availW / w, availH / h)
    setFitScale(scale)
    setView({
      scale,
      x: (stage.clientWidth - w * scale) / 2,
      y: (stage.clientHeight - FIT_MARGIN_BOTTOM - h * scale) / 2,
    })
  }, [])

  // 打开（层挂载）后同步测量适配；内容尺寸就绪（img 加载等）经 RO 复适配
  useLayoutEffect(() => {
    if (!open) return
    fit()
  }, [open, fit, children])

  useEffect(() => {
    if (!open) return
    const ro = new ResizeObserver(() => fit())
    if (stageRef.current) ro.observe(stageRef.current)
    if (contentRef.current) ro.observe(contentRef.current)
    return () => ro.disconnect()
  }, [open, fit])

  // Esc 关闭 + 页面滚动锁定
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [open, onClose])

  // 滚轮缩放：非被动监听才能 preventDefault（阻止遮罩后面页面滚动）
  useEffect(() => {
    if (!open) return
    const stage = stageRef.current
    if (!stage) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const factor = e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP
      const cx = e.clientX - stage.getBoundingClientRect().left
      const cy = e.clientY - stage.getBoundingClientRect().top
      setView((v) => {
        if (!v) return v
        const scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, v.scale * factor))
        // 光标锚点保持不动：t' = c − (c − t) · s'/s
        return {
          scale,
          x: cx - ((cx - v.x) * scale) / v.scale,
          y: cy - ((cy - v.y) * scale) / v.scale,
        }
      })
    }
    stage.addEventListener('wheel', onWheel, { passive: false })
    return () => stage.removeEventListener('wheel', onWheel)
  }, [open])

  if (!open) return null

  // 拖拽平移：down/move/up 全挂在 stage（= pointer capture 目标）。capture 后
  // 事件重定向到 stage，若处理器挂在内容层会收不到 pointerup（down 残留 →
  // 之后悬停也被当作拖拽，图像跟着鼠标跑）。工具条区域内按下不启动平移，
  // 保证按钮 click 不被 capture 吞掉。
  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    const stage = stageRef.current
    if (!stage) return
    if ((e.target as Element).closest('.zoom-stage__toolbar')) return
    // preventDefault：阻断原生图片拖拽/文字选取（平移由 pointer capture 接管）
    e.preventDefault()
    dragRef.current = {
      down: true,
      moved: false,
      x: e.clientX,
      y: e.clientY,
      vx: view?.x ?? 0,
      vy: view?.y ?? 0,
    }
    stage.setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d.down) return
    const dx = e.clientX - d.x
    const dy = e.clientY - d.y
    if (!d.moved && Math.hypot(dx, dy) < 4) return
    d.moved = true
    setView((v) => (v ? { ...v, x: d.vx + dx, y: d.vy + dy } : v))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    dragRef.current.down = false
    const stage = stageRef.current
    if (stage?.hasPointerCapture(e.pointerId)) stage.releasePointerCapture(e.pointerId)
  }

  /** 按钮缩放：以舞台中心为锚 */
  const zoomBy = (factor: number) => {
    const stage = stageRef.current
    setView((v) => {
      if (!v) return v
      const scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, v.scale * factor))
      const cx = (stage?.clientWidth ?? 0) / 2
      const cy = (stage?.clientHeight ?? 0) / 2
      return {
        scale,
        x: cx - ((cx - v.x) * scale) / v.scale,
        y: cy - ((cy - v.y) * scale) / v.scale,
      }
    })
  }

  const pct = view ? Math.round((view.scale / fitScale) * 100) : null

  return createPortal(
    <div
      className="zoom-stage"
      ref={stageRef}
      role="dialog"
      aria-modal="true"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={(e) => {
        // 双击复位（capture 后 click 系列重定向到 stage，故挂在 stage；工具条双击不处理）
        if ((e.target as Element).closest('.zoom-stage__toolbar')) return
        fit()
      }}
    >
      {/* 遮罩点击关闭：拖拽收尾的 click 不关（moved 标记在下次按下时复位） */}
      <div
        className="zoom-stage__backdrop"
        onClick={() => {
          if (!dragRef.current.moved) onClose()
        }}
      />
      <div
        ref={contentRef}
        className={`zoom-stage__content svg-scope${block ? ' zoom-stage__content--block' : ''}`}
        style={
          view
            ? { transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }
            : { visibility: 'hidden' }
        }
      >
        {children}
      </div>
      <div className="zoom-stage__toolbar">
        <button type="button" className="zoom-stage__btn" aria-label="缩小" onClick={() => zoomBy(1 / ZOOM_STEP)}>
          <ZoomOut size={15} />
        </button>
        <span className="zoom-stage__pct">{pct != null ? `${pct}%` : ''}</span>
        <button type="button" className="zoom-stage__btn" aria-label="放大" onClick={() => zoomBy(ZOOM_STEP)}>
          <ZoomIn size={15} />
        </button>
        <button type="button" className="zoom-stage__btn zoom-stage__btn--fit" aria-label="适配窗口" onClick={fit}>
          <Maximize2 size={14} />
        </button>
        <button type="button" className="zoom-stage__btn" aria-label="关闭" onClick={onClose}>
          <X size={15} />
        </button>
      </div>
    </div>,
    document.body,
  )
}
