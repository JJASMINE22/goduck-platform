/* Markdown 渲染 —— agent 回复 / rubric / 附件清单展示。
   react-markdown + gfm + 代码高亮 + 数学公式（remark-math → KaTeX）。
   流式期（streaming prop）：remend 先补全半截语法（加粗/行内代码/链接/$$ 等），
   配合尾沿节流重解析实现「边流边排版」；mermaid/svg 块走语法/净化门禁渐进渲染
   （MdStreamingContext 区分流式中与终态）。
   代码块带「复制」；mermaid 块带「下载 SVG / 复制源码」；svg 围栏走 SvgFigure
   （净化 + 作用域主题化，见 svgScope.css）；图像带「下载」。
   mermaid/svg 均走单一渲染入口（准则 F7）。未启用 rehype-raw：内联 HTML 不渲染。 */
import { type ReactElement, type ReactNode, memo, useContext, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown, { type Components, type Options } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkBreaks from 'remark-breaks'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import remend from 'remend'
import 'katex/dist/katex.min.css'
import { renderMermaidInto, validateMermaid } from '@/lib/mermaid'
import { ensureCanvasBackground } from './svgNormalize'
import { SvgFigure } from './SvgFigure'
import { ZoomStage } from './ZoomStage'
import { MdStreamingContext } from './mdStreaming'
import { panHandlers, panState } from './dragPan'
import { useThemeStore } from '@/store/themeStore'

/** LLM 常把公式写作 \(..\)/\[..\]，remark-math 只认 $..$/$$..$$。
    先用占位符保护代码围栏与行内代码，再统一替换定界符，避免误伤代码。 */
function normalizeMathDelimiters(src: string): string {
  const guarded: string[] = []
  const masked = src.replace(/```[\s\S]*?(?:```|$)|`[^`\n]*`/g, (m) => {
    guarded.push(m)
    return `\uE000${guarded.length - 1}\uE000`
  })
  return masked
    .replace(/\\\[([\s\S]*?)\\\]/g, (_m, body: string) => `$$${body}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_m, body: string) => `$${body}$`)
    .replace(/\uE000(\d+)\uE000/g, (_m, i: string) => guarded[Number(i)] ?? '')
}

/** 递归提取 React 节点的纯文本（代码源码 / mermaid 源码复制用，hljs span 也能拼回原文）。 */
function extractText(node: unknown): string {
  if (node == null || node === true || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (typeof node === 'object' && 'props' in (node as Record<string, unknown>)) {
    return extractText((node as { props?: { children?: unknown } }).props?.children)
  }
  return ''
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/** 代码块：顶栏（语言 + 复制） + 高亮正文。复制内容取自源码（extractText）。 */
function CodeBlock({ lang, source, children }: { lang: string; source: string; children: ReactNode }): ReactNode {
  const [copied, setCopied] = useState(false)
  const onCopy = async () => {
    if (await copyText(source)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="md-codeblock">
      <div className="md-codeblock__bar">
        <span className="md-codeblock__lang">{lang || 'text'}</span>
        <button type="button" className="md-codeblock__btn" onClick={onCopy}>
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="md-codeblock__pre">{children}</pre>
    </div>
  )
}

/** mermaid 块渐进渲染节拍：流式增长中半截语法 parse 必失败，防抖等待即可；
    parse 通过 = 语法自洽（长图会在流式中段「渐现」，短图在围栏闭合后渲出） */
const MERMAID_DEBOUNCE_MS = 300

/** mermaid 块：顶栏（复制源码 + 下载 SVG） + 渲染图。SVG 序列化自渲染容器内的 <svg>。
    theme 入依赖：Light|Dark 切换后以对应 mermaid 主题重渲染。
    渲染门禁：validateMermaid 通过才渲（失败静默等待）；流式中语法回退非法时
    保留上一帧已渲图，首帧成功前以占位文案区分「生成中」与「终态解析失败」。 */
function MermaidBlock({ code }: { code: string }): ReactNode {
  const ref = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  const [rendered, setRendered] = useState(false)
  // 放大内容：打开瞬间快照已渲 innerHTML（含 mermaid 内联样式，主题色已烘焙），
  // 不重跑渲染管线；null = 关闭
  const [zoomHtml, setZoomHtml] = useState<string | null>(null)
  const streaming = useContext(MdStreamingContext)
  const theme = useThemeStore((s) => s.theme)
  const lastRenderRef = useRef<{ code: string; theme: string } | null>(null)
  useEffect(() => {
    let active = true
    // 主题切换（代码未变）即时重渲；代码变化走防抖 + 语法门禁
    const themeOnly = lastRenderRef.current?.code === code && lastRenderRef.current.theme !== theme
    const timer = setTimeout(() => {
      void (async () => {
        if (!(await validateMermaid(code))) return
        if (!active) return
        await renderMermaidInto(ref.current, code)
        if (!active) {
          if (ref.current) ref.current.innerHTML = ''
          return
        }
        lastRenderRef.current = { code, theme }
        setRendered(true)
      })()
    }, themeOnly ? 0 : MERMAID_DEBOUNCE_MS)
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [code, theme])
  const onCopySource = async () => {
    if (await copyText(code)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  const onDownloadSvg = () => {
    const svg = ref.current?.querySelector('svg')
    if (!svg) return
    // 克隆后垫整幅画布底再序列化：独立文件脱离页面容器，暗色主题下浅文字在白底查看器不可见
    const clone = svg.cloneNode(true) as SVGSVGElement
    ensureCanvasBackground(clone)
    const xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + new XMLSerializer().serializeToString(clone)
    triggerDownload(new Blob([xml], { type: 'image/svg+xml;charset=utf-8' }), 'mermaid.svg')
  }
  // 打开放大层：快照当前已渲图表（未渲完成时无 svg 可取，不打开）
  const onZoom = () => {
    if (ref.current?.querySelector('svg')) setZoomHtml(ref.current.innerHTML)
  }
  return (
    <div className="md-mermaid">
      <div className="md-mermaid__bar">
        <span>mermaid</span>
        <button type="button" className="md-codeblock__btn" onClick={onCopySource}>
          {copied ? '源码已复制' : '复制源码'}
        </button>
        <button type="button" className="md-codeblock__btn" onClick={onDownloadSvg}>
          下载 SVG
        </button>
        <button type="button" className="md-codeblock__btn" onClick={onZoom} disabled={!rendered}>
          放大
        </button>
      </div>
      {/* 首帧成功前不挂画布（空 body 占位条）；占位与画布互斥，innerHTML 直写不与
          React 子树混管 */}
      {!rendered ? (
        <div className="md-deferred">{streaming ? '图表生成中…' : '图表解析失败，可复制源码排查'}</div>
      ) : null}
      <div
        ref={ref}
        className="md-mermaid__body"
        style={rendered ? undefined : { display: 'none' }}
        {...panHandlers}
        onClick={() => {
          // 拖拽平移的收尾 click 不触发放大（同 SvgFigure）
          if (panState.panning) return
          onZoom()
        }}
      />
      <ZoomStage open={zoomHtml != null} onClose={() => setZoomHtml(null)} block>
        <div dangerouslySetInnerHTML={{ __html: zoomHtml ?? '' }} />
      </ZoomStage>
    </div>
  )
}

/** 图像：渲染 + 悬浮「下载」按钮（同源/data URL 直接下载；跨源需后端代理，故 best-effort）。
    点击图像 → 中央放大查看（滚轮缩放/拖拽平移，与 mermaid/SVG 同一查看层）。 */
function MarkdownImage({ src, alt }: { src?: string; alt?: string }): ReactNode {
  const [zoom, setZoom] = useState(false)
  const onDownload = async () => {
    if (!src) return
    try {
      const res = await fetch(src)
      const blob = await res.blob()
      const name = (alt || 'image').replace(/[^\w.-]+/g, '_')
      const ext = blob.type.split('/')[1] || 'png'
      triggerDownload(blob, `${name}.${ext}`)
    } catch {
      // 跨源 fetch 失败：退化为直接打开链接
      window.open(src, '_blank')
    }
  }
  return (
    <span className="md-img">
      <img src={src} alt={alt} onClick={() => src && setZoom(true)} />
      <button type="button" className="md-img__dl" onClick={onDownload} title="下载图像">
        下载
      </button>
      <ZoomStage open={zoom} onClose={() => setZoom(false)}>
        <img src={src} alt={alt} />
      </ZoomStage>
    </span>
  )
}

/** breaks：单次换行渲染为 <br>（所见即所得）。用于用户气泡——非技术用户
    不会写"行尾两空格"或空行分段，GFM 默认会把多行合并成一段；
    同时保留加粗/列表/代码等轻量语法，懂 markdown 的用户不受影响。

    性能：插件表与组件映射提为模块常量、渲染内核独立 memo（MdRender）——
    src 不变即完全跳过重渲染，remark→rehype（gfm/math/highlight/katex）
    全管线不重跑。流式合并批（≈30fps）只更新引用，src 由上层节流提交，
    历史消息与节拍间隙的消息零重解析。 */
const REMARK_PLUGINS: Options['remarkPlugins'] = [remarkGfm, remarkMath]
const REMARK_PLUGINS_BREAKS: Options['remarkPlugins'] = [remarkGfm, remarkMath, remarkBreaks]
const REHYPE_PLUGINS: Options['rehypePlugins'] = [
  // ignoreMissing：mermaid 不是已注册高亮语言，避免 rehype-highlight 抛错
  [rehypeHighlight, { ignoreMissing: true }],
  // 出错不上抛：非法 LaTeX 在原位渲染红色错误提示，不中断整条消息
  [rehypeKatex, { error: false, strict: 'ignore', output: 'htmlAndMathml' }],
]
const MD_COMPONENTS: Components = {
  pre: ({ children }) => {
    const codeEl = (Array.isArray(children) ? children[0] : children) as
      | ReactElement<{ className?: string; children?: ReactNode }>
      | undefined
    const className: string = codeEl?.props?.className || ''
    const match = /language-(\w+)/.exec(className)
    const source = extractText(codeEl?.props?.children)
    const lang = match?.[1] ?? ''
    if (lang === 'mermaid') return <MermaidBlock code={source} />
    // svg 围栏专用块；xml/html 语言且正文以 <svg 开头也视为 SVG（模型常见写法兜底）
    const looksLikeSvg = source.trimStart().slice(0, 5).toLowerCase() === '<svg'
    if (lang === 'svg' || ((lang === 'xml' || lang === 'html') && looksLikeSvg)) {
      return <SvgFigure code={source} />
    }
    return (
      <CodeBlock lang={lang} source={source}>
        {children}
      </CodeBlock>
    )
  },
  code: ({ className, children, ...props }) => (
    <code {...props} className={className} style={{ fontFamily: 'var(--font-mono)', fontSize: '0.92em' }}>
      {children}
    </code>
  ),
  img: ({ src, alt }) => <MarkdownImage src={src} alt={alt} />,
  /* remend 把流式半截链接补为 [text](streamdown:incomplete-link)：占位 href
     中和为纯文本（无悬停链接感、不产生无效导航），url 到齐后自然恢复 */
  a: ({ href, children, ...props }) =>
    href === 'streamdown:incomplete-link' ? <span>{children}</span> : <a {...props} href={href}>{children}</a>,
  /* 表格：横向滚动壳（.md-table-wrap）+ 拖拽平移兜住宽表——表格取 max-content
     自然宽度（宽度规则见 index.css，width:auto 会被钳回容器宽导致列压扁、
     滚动条永不出现）；td/th 原生样式不变 */
  table: (props) => (
    <div className="md-table-wrap" {...panHandlers}>
      <table {...props} style={{ borderCollapse: 'collapse', margin: 0 }} />
    </div>
  ),
  th: (props) => (
    <th {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px', background: 'var(--color-border-soft)' }} />
  ),
  td: (props) => <td {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px' }} />,
}

/** 渲染内核隔离（memo）：src 不变即跳过 remark→rehype 全管线 */
const MdRender = memo(function MdRender({ src, breaks }: { src: string; breaks: boolean }): ReactNode {
  return (
    <ReactMarkdown
      remarkPlugins={breaks ? REMARK_PLUGINS_BREAKS : REMARK_PLUGINS}
      rehypePlugins={REHYPE_PLUGINS}
      components={MD_COMPONENTS}
    >
      {src}
    </ReactMarkdown>
  )
})

/** 流式重解析节流间隔：吐字观感与解析开销的平衡点 */
const STREAM_THROTTLE_MS = 120

export const Markdown = memo(function Markdown({
  children,
  breaks = false,
  streaming = false,
}: {
  children: string
  breaks?: boolean
  /** true=token 流式接收中：remend 补全半截语法 + 节流重解析，全程 Markdown 排版 */
  streaming?: boolean
}): ReactNode {
  // 流式视图节流：合并批（≈30fps）仅更新引用，src 按节拍（120ms）提交——
  // 吐字观感几乎无损，remark→rehype 全管线重跑频率有界。
  // 终态直接从 props 渲染（不经 state），流式→终态切换无同步开销
  const [view, setView] = useState(children)
  const committedAt = useRef(0)
  useEffect(() => {
    if (!streaming) return
    // 尾沿节流：等待时长按上次提交点推算，批间反复重排不推迟提交点、不丢尾帧
    const wait = Math.max(0, STREAM_THROTTLE_MS - (Date.now() - committedAt.current))
    const timer = setTimeout(() => {
      committedAt.current = Date.now()
      setView(children)
    }, wait)
    return () => clearTimeout(timer)
  }, [children, streaming])
  const src = useMemo(
    () => (streaming ? remend(normalizeMathDelimiters(view)) : normalizeMathDelimiters(children)),
    [streaming, view, children],
  )
  return (
    <div className="gd-md" style={{ lineHeight: 1.7, wordBreak: 'break-word' }}>
      <MdStreamingContext.Provider value={streaming}>
        <MdRender src={src} breaks={breaks} />
      </MdStreamingContext.Provider>
    </div>
  )
})
