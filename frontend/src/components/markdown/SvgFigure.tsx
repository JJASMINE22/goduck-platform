/* SvgFigure —— 聊天内联 SVG 渲染（archify 方法子集：净化 + 作用域主题化）。
   安全底线：DOMPurify 白名单净化（禁 script/foreignObject/事件处理器/外链资源），
   不启用 rehype-raw 的全局内联 HTML——SVG 只经 ```svg 围栏这一受控入口进来。
   呈现：.svg-scope 注入语义变量（深浅主题随 tokens.css 自动跟随）+ 点阵画布底；
   未走 svg-diagram 技能的硬编码色由 svgNormalize 色相保留归一化接管（明度随主题）。
   工具栏与 MermaidBlock 同语言（复制源码 / 下载 SVG / 放大）。
   下载：克隆 svg + 内嵌「变量已解析为具体色值」的作用域样式（svgScope.css ?raw），
   独立文件在任意浏览器打开同样带色。 */
import DOMPurify from 'dompurify'
import { memo, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { ensureCanvasBackground, normalizeSvgColors } from './svgNormalize'
import { panHandlers, panState } from './dragPan'
import { MdStreamingContext } from './mdStreaming'
import { ZoomStage } from './ZoomStage'
import './svgScope.css'
import scopeCssRaw from './svgScope.css?raw'

/** 净化 + 自适应处理。返回可直接注入的 SVG 源码；无法解析返回 null（组件降级为源码展示）。 */
function prepareSvg(code: string): string | null {
  let clean: string
  try {
    clean = DOMPurify.sanitize(code, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ['script', 'foreignObject'],
      FORBID_ATTR: ['src', 'href', 'xlink:href'],
    })
  } catch {
    return null
  }
  const doc = new DOMParser().parseFromString(clean, 'image/svg+xml')
  // documentElement 类型库固定为 HTMLElement；下方 nodeName 校验后即为 svg 根，收窄类型
  const svg = doc.documentElement as unknown as SVGSVGElement
  if (!svg || svg.nodeName.toLowerCase() !== 'svg' || doc.getElementsByTagName('parsererror').length) {
    return null
  }
  // 有 viewBox 才可安全缩放：去掉定死的 width/height（CSS max-width:100% 接管）；
  // 无 viewBox 保留原始尺寸，容器横向滚动兜底。
  if (svg.getAttribute('viewBox')) {
    svg.removeAttribute('width')
    svg.removeAttribute('height')
  }
  svg.classList.add('svg-scope')
  // 硬编码色归一化（色相保留、明度交主题令牌）；合规 SVG 零改动穿过
  normalizeSvgColors(svg)
  return new XMLSerializer().serializeToString(svg)
}

/** 把 CSS 中的 var(--token) 解析为当前主题下的具体色值（两轮替换覆盖一层嵌套）。
 *  下载的独立 SVG 不依赖页面环境也能正确着色。 */
function resolveCssVars(css: string): string {
  const cs = getComputedStyle(document.documentElement)
  const once = (s: string): string =>
    s.replace(/var\((--[\w-]+)(?:\s*,\s*([^()]+))?\)/g, (_m, name: string, fb: string) => {
      const v = cs.getPropertyValue(name).trim()
      return v || fb || 'transparent'
    })
  return once(once(css))
}

/** 组装独立可打开的 SVG 文件：克隆 + 顶部内嵌已解析变量的作用域样式；
 *  归一化写入 style 属性的主题表达式同样解析为当前主题具体色值，并垫整幅画布底。 */
function buildStandaloneSvg(svg: SVGSVGElement): string {
  const clone = svg.cloneNode(true) as SVGSVGElement
  clone.querySelectorAll('[style]').forEach((el) => {
    const s = el.getAttribute('style')
    if (s) el.setAttribute('style', resolveCssVars(s))
  })
  ensureCanvasBackground(clone)
  const style = document.createElementNS('http://www.w3.org/2000/svg', 'style')
  style.textContent = resolveCssVars(scopeCssRaw)
  clone.insertBefore(style, clone.firstChild)
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  return '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone)
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function triggerDownload(content: string, filename: string): void {
  const blob = new Blob([content], { type: 'image/svg+xml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export const SvgFigure = memo(function SvgFigure({ code }: { code: string }): ReactNode {
  // 净化结果缓存：内容不变不重跑（流式围栏闭合后内容即稳定）
  const prepared = useMemo(() => prepareSvg(code), [code])
  const streaming = useContext(MdStreamingContext)
  const bodyRef = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  const [zoom, setZoom] = useState(false)

  const onCopySource = async () => {
    if (await copyText(code)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  const onDownload = () => {
    const svg = bodyRef.current?.querySelector('svg')
    if (svg) triggerDownload(buildStandaloneSvg(svg as SVGSVGElement), 'diagram.svg')
  }

  // 无法净化/解析：降级为源码展示（不吞内容，用户仍可复制排查）。
  // 流式半截 SVG（XML 严格解析对未闭合标签必失败）同走此降级，闭合后自动转图
  if (prepared === null) {
    return (
      <div className="md-svg">
        <div className="md-svg__bar">
          <span>{streaming ? 'svg · 生成中…' : 'svg'}</span>
          <button type="button" className="md-codeblock__btn" onClick={onCopySource}>
            {copied ? '源码已复制' : '复制源码'}
          </button>
        </div>
        <pre className="md-svg__fallback">{code}</pre>
      </div>
    )
  }

  return (
    <div className="md-svg">
      <div className="md-svg__bar">
        <span>svg</span>
        <button type="button" className="md-codeblock__btn" onClick={onCopySource}>
          {copied ? '源码已复制' : '复制源码'}
        </button>
        <button type="button" className="md-codeblock__btn" onClick={onDownload}>
          下载 SVG
        </button>
        <button type="button" className="md-codeblock__btn" onClick={() => setZoom(true)}>
          放大
        </button>
      </div>
      <div
        ref={bodyRef}
        className="md-svg__body svg-scope"
        {...panHandlers}
        onClick={() => {
          // 拖拽平移的收尾 click 不触发放大（pointerdown 已复位 panning，
          // 其后的正常点击不受影响）
          if (panState.panning) return
          setZoom(true)
        }}
        role="button"
        tabIndex={0}
        aria-label="放大查看 SVG 图表"
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') setZoom(true)
        }}
        dangerouslySetInnerHTML={{ __html: prepared }}
      />
      {/* 中央放大查看层：滚轮缩放/拖拽平移，打开即适配窗口（比气泡更大，而非更小） */}
      <ZoomStage open={zoom} onClose={() => setZoom(false)} block>
        <div dangerouslySetInnerHTML={{ __html: prepared }} />
      </ZoomStage>
    </div>
  )
})
