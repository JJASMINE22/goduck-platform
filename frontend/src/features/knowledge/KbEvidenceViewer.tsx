/* 命中证据锚定 viewer（plan: kb-docling-parse-display D2）。
   - 单 viewer 实例加载预签名原始 PDF：disableAutoFetch + HTTP Range 按页拉取，
     大文件不全量下载（MinIO 预签名原生支持 Range）。
   - SVG 叠框 = 归一化 bbox × viewport：viewBox 0..100 非均匀铺满画布，与
     bbox 归一化（左上原点 0..1）同一几何模型；viewport 自带页旋转
     （/Rotate 90 直角命中），双栏页锚定由 docling 阅读序 bbox 保证。
    - 同文档多命中聚合：右侧锚点列表页间跳转，主锚点（命中本体）与图片资产
      锚点区分标识；bbox 缺失的锚点仅跳页不画框。
    - 同页同 bbox 锚点分组画框（预算切分子单元共享父 bbox，逐条叠画会完全
      重合），组内 >1 时玻璃角标计数；组内精确选择走右侧列表，列表随
      activeId 滚动联动。
    - Chrome（弹层/工具胶囊/锚点面板/条目/角标）为流体玻璃风格（.kb-ev-*，
      index.css，复用 .glass-btn 成像配方：半透底 + blur/saturate 取景 +
      rim 高光 + 流光）；PDF 纸面井（.kb-ev-well）保持近实底内凹保可读性。
    - 预签名 URL 有时效（1h）：过期重新检索获取，不在此续签。 */
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { Button, Flex, Modal, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import {
  ChevronLeft,
  ChevronRight,
  Crosshair,
  FileWarning,
  Image as ImageIcon,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { KbBbox } from '@/types'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

/** 单个可定位锚点（命中本体或其绑定图片资产） */
export interface EvidenceAnchor {
  id: string
  page: number
  bbox?: KbBbox | null
  /** 列表项标题（命中摘要首行 / 图注） */
  title: string
  meta?: string
}

/**
 * 挂载即加载（父组件以 docUrl 作 key 条件渲染 → 每次定位均为新实例），
 * 卸载即销毁文档；docUrl/anchors/initialAnchorId 对实例不可变。
 */
export function KbEvidenceViewer({
  onClose,
  docUrl,
  docName,
  anchors,
  initialAnchorId,
}: {
  onClose: () => void
  docUrl: string
  docName: string
  anchors: EvidenceAnchor[]
  initialAnchorId?: string
}): ReactNode {
  const docRef = useRef<pdfjsLib.PDFDocumentProxy | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const renderTaskRef = useRef<pdfjsLib.RenderTask | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [numPages, setNumPages] = useState(0)
  const [pageNo, setPageNo] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [wrapWidth, setWrapWidth] = useState(0)
  const [activeId, setActiveId] = useState<string | null>(null)

  // 文档加载（挂载一次；卸载销毁）。setState 全部位于异步回调，无级联渲染。
  useEffect(() => {
    let cancelled = false
    const task = pdfjsLib.getDocument({
      url: docUrl,
      disableAutoFetch: true, // Range 按需取页，不全量下载
      disableStream: false,
    })
    task.promise
      .then((doc) => {
        if (cancelled) {
          void doc.destroy()
          return
        }
        docRef.current = doc
        setNumPages(doc.numPages)
        // anchors[0] 回退仅为组件 API 容错的最后防线：调用方（聊天卡/检索调试）
        // 已保证 initialAnchorId 指向被点击命中自己的锚点（主锚点缺失时回落
        // 其首个资产锚点），正常路径不会走到这里
        const first = anchors.find((a) => a.id === initialAnchorId) ?? anchors[0]
        setPageNo(Math.max(1, first?.page ?? 1))
        setActiveId(first?.id ?? null)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String((e as Error)?.message ?? e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
      docRef.current = null
      void task.destroy()
    }
    // 实例级不可变 props（keyed 挂载）：挂载即加载，无需响应变更
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 画布宽度自适应（容器 resize → 重渲染；订阅回调内 setState）
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0
      if (w > 0) setWrapWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 页渲染（页码/缩放/容器宽变化；取消在途任务防叠画）
  useEffect(() => {
    const doc = docRef.current
    const canvas = canvasRef.current
    if (!doc || !canvas || !wrapWidth || pageNo < 1) return
    let cancelled = false
    ;(async () => {
      try {
        const page = await doc.getPage(Math.min(pageNo, doc.numPages))
        if (cancelled) return
        const base = page.getViewport({ scale: 1 })
        const scale = (wrapWidth / base.width) * zoom
        const viewport = page.getViewport({ scale })
        const dpr = window.devicePixelRatio || 1
        canvas.width = Math.floor(viewport.width * dpr)
        canvas.height = Math.floor(viewport.height * dpr)
        canvas.style.width = `${Math.floor(viewport.width)}px`
        canvas.style.height = `${Math.floor(viewport.height)}px`
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        renderTaskRef.current?.cancel()
        // pdfjs v5：RenderParameters 直接收 canvas（dpr 经 transform 缩放）
        const renderTask = page.render({
          canvas,
          canvasContext: ctx,
          viewport,
          transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined,
        })
        renderTaskRef.current = renderTask
        await renderTask.promise
      } catch {
        /* cancel 引起的 AbortException 属预期 */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [numPages, pageNo, zoom, wrapWidth])

  useEffect(() => {
    return () => {
      renderTaskRef.current?.cancel()
      void docRef.current?.destroy()
    }
  }, [])

  const jumpTo = useCallback((a: EvidenceAnchor) => {
    setPageNo(Math.max(1, a.page))
    setActiveId(a.id)
  }, [])

  const pageAnchors = useMemo(
    () => anchors.filter((a) => a.page === pageNo),
    [anchors, pageNo]
  )

  // 同页同 bbox 锚点分组：预算切分的兄弟单元共享父 bbox，逐条叠画会完全
  // 重合（视觉只见一框且点击互挡）。每组画一框，组内 >1 时角标计数；
  // 组内精确选择走右侧列表（列表保持逐条平铺，不分组）。
  const boxGroups = useMemo(() => {
    const groups: { key: string; anchors: EvidenceAnchor[] }[] = []
    const byKey = new Map<string, { key: string; anchors: EvidenceAnchor[] }>()
    for (const a of pageAnchors) {
      if (!a.bbox) continue
      const key = `${a.bbox.x0},${a.bbox.y0},${a.bbox.x1},${a.bbox.y1}`
      let g = byKey.get(key)
      if (!g) {
        g = { key, anchors: [] }
        byKey.set(key, g)
        groups.push(g)
      }
      g.anchors.push(a)
    }
    return groups
  }, [pageAnchors])

  // 右列表与画布联动：activeId 变化（挂载初值 / 点框 / 点列表）滚动对应项入视
  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!activeId) return
    // 转义引号/反斜杠，防御 id 意外包含选择器语法（镜像 TurnRail 纪律）
    const sel = `[data-anchor-id="${activeId.replace(/["\\]/g, '\\$&')}"]`
    listRef.current?.querySelector(sel)?.scrollIntoView({
      behavior: 'smooth',
      block: 'nearest',
    })
  }, [activeId])

  return (
    <Modal
      open
      onCancel={onClose}
      footer={null}
      width="94vw"
      centered
      destroyOnClose
      rootClassName="kb-ev-root"
      className="kb-ev-modal"
      title={
        <Space size={8}>
          <Crosshair size={15} />
          <span>证据锚定：{docName}</span>
          <Typography.Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
            原始 PDF · {numPages || '…'} 页 · {anchors.length} 个锚点
          </Typography.Text>
        </Space>
      }
    >
      <Flex gap={12} align="stretch" style={{ height: '78vh' }}>
        {/* 左：画布 + SVG 叠框 */}
        <Flex vertical gap={8} style={{ flex: 1, minWidth: 0 }}>
          {/* 工具胶囊（流体玻璃）：翻页/页码/缩放/本页锚点悬浮条 */}
          <Flex gap={2} align="center" className="kb-ev-toolbar">
            <Button
              size="small"
              icon={<ChevronLeft size={14} />}
              disabled={pageNo <= 1}
              onClick={() => setPageNo((p) => Math.max(1, p - 1))}
            />
            <Typography.Text style={{ minWidth: 72, textAlign: 'center' }}>
              {pageNo} / {numPages || '…'}
            </Typography.Text>
            <Button
              size="small"
              icon={<ChevronRight size={14} />}
              disabled={numPages > 0 && pageNo >= numPages}
              onClick={() => setPageNo((p) => (numPages ? Math.min(numPages, p + 1) : p))}
            />
            <Button
              size="small"
              icon={<ZoomOut size={14} />}
              disabled={zoom <= 0.5}
              onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.2).toFixed(2)))}
            />
            <Button
              size="small"
              icon={<ZoomIn size={14} />}
              disabled={zoom >= 3}
              onClick={() => setZoom((z) => Math.min(3, +(z + 0.2).toFixed(2)))}
            />
            {pageAnchors.length > 0 ? (
              <Tag color="blue" style={{ marginInlineStart: 4 }}>
                本页 {pageAnchors.length} 锚点
              </Tag>
            ) : null}
          </Flex>
          {/* 纸面井：近实底内凹底座（玻璃 chrome 的衬托，样式见 .kb-ev-well） */}
          <div ref={wrapRef} className="kb-ev-well">
            {error ? (
              <Flex vertical align="center" justify="center" gap={8} style={{ margin: 'auto' }}>
                <FileWarning size={28} />
                <Typography.Text type="danger">PDF 加载失败：{error}</Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  预签名 URL 可能已过期（时效 1h），请重新检索后再定位
                </Typography.Text>
              </Flex>
            ) : loading ? (
              <Flex vertical align="center" justify="center" gap={8} style={{ margin: 'auto' }}>
                <Spin />
                <Typography.Text type="secondary">加载原始 PDF…</Typography.Text>
              </Flex>
            ) : (
              <div
                style={{
                  position: 'relative',
                  display: 'inline-block',
                  lineHeight: 0,
                  margin: 'auto',
                }}
              >
                {/* margin:auto 防止 wrap 的 flex 默认 align-items:stretch 把包裹层
                    拉成可视区高度（否则绝对定位 SVG 只盖住 canvas 顶部一段，
                    bbox 叠框整体向顶部压缩）；canvas 小于视口时自动居中，
                    溢出时贴左保持可滚动（justify-content:center 的左侧溢出不可达） */}
                <canvas ref={canvasRef} />
                <svg
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                  style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
                >
                  {boxGroups.map((g) => {
                    const b = g.anchors[0].bbox!
                    const active = g.anchors.some((a) => a.id === activeId)
                    return (
                      <rect
                        key={g.key}
                        x={b.x0 * 100}
                        y={b.y0 * 100}
                        width={Math.max(0.2, (b.x1 - b.x0) * 100)}
                        height={Math.max(0.2, (b.y1 - b.y0) * 100)}
                        fill={active ? 'rgba(22,119,255,0.18)' : 'rgba(82,196,26,0.14)'}
                        stroke={active ? '#1677ff' : '#52c41a'}
                        strokeWidth={active ? 0.7 : 0.4}
                        vectorEffect="non-scaling-stroke"
                        style={{ cursor: 'pointer' }}
                        // 点组选组：组内已有选中则保持（蓝显），否则选组内首个；
                        // 共享区域的精确选择走右侧列表
                        onClick={() =>
                          setActiveId(
                            g.anchors.find((a) => a.id === activeId)?.id ?? g.anchors[0].id,
                          )
                        }
                      />
                    )
                  })}
                </svg>
                {/* 同区域计数角标：HTML 层实现（SVG text 在 preserveAspectRatio=none
                    下会被非均匀拉伸变形），百分比定位与 SVG viewBox 同几何模型；
                    pointerEvents:none 保证不挡框点击 */}
                {boxGroups
                  .filter((g) => g.anchors.length > 1)
                  .map((g) => {
                    const b = g.anchors[0].bbox!
                    // 贴顶框角标翻到框内，避免溢出画布上缘
                    const inside = b.y0 < 0.05
                    return (
                      <div
                        key={`badge-${g.key}`}
                        className="kb-ev-badge"
                        style={{
                          left: `${b.x0 * 100}%`,
                          top: `${b.y0 * 100}%`,
                          transform: inside ? 'none' : 'translateY(-100%)',
                        }}
                      >
                        {g.anchors.length} 条命中同区域
                      </div>
                    )
                  })}
              </div>
            )}
          </div>
        </Flex>

        {/* 右：锚点列表（同文档多命中聚合，页间跳转；随 activeId 滚动联动）——
            流体玻璃面板（.kb-ev-panel），条目为玻璃小片（.kb-ev-item） */}
        <div ref={listRef} className="kb-ev-panel">
          <Typography.Text strong style={{ fontSize: 13 }}>
            命中锚点（{anchors.length}）
          </Typography.Text>
          <Flex vertical gap={6} style={{ marginTop: 8 }}>
            {anchors.map((a) => (
              <div
                key={a.id}
                data-anchor-id={a.id}
                onClick={() => jumpTo(a)}
                className={a.id === activeId ? 'kb-ev-item kb-ev-item--active' : 'kb-ev-item'}
                tabIndex={0}
                role="button"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') jumpTo(a)
                }}
              >
                <Space size={4} wrap>
                  {a.id.includes(':a') ? (
                    <Tooltip title="图片资产锚点">
                      <ImageIcon size={12} style={{ color: 'var(--color-text-tertiary)' }} />
                    </Tooltip>
                  ) : (
                    <Crosshair size={12} style={{ color: 'var(--color-text-tertiary)' }} />
                  )}
                  <Tag style={{ marginInlineStart: 0 }}>P{a.page}</Tag>
                  {a.meta ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {a.meta}
                    </Typography.Text>
                  ) : null}
                </Space>
                <Typography.Paragraph
                  ellipsis={{ rows: 2 }}
                  style={{ margin: '4px 0 0', fontSize: 12 }}
                >
                  {a.title}
                </Typography.Paragraph>
              </div>
            ))}
            {anchors.length === 0 ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                无锚点（bbox 缺失的命中仅支持文本卡片）
              </Typography.Text>
            ) : null}
          </Flex>
        </div>
      </Flex>
    </Modal>
  )
}
