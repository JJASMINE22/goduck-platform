/* Markdown 渲染 —— agent 回复 / rubric / 附件清单展示。
   react-markdown + gfm + 代码高亮 + 数学公式（remark-math → KaTeX）。
   代码块带「复制」；mermaid 块带「下载 SVG / 复制源码」；svg 围栏走 SvgFigure
   （净化 + 作用域主题化，见 svgScope.css）；图像带「下载」。
   mermaid/svg 均走单一渲染入口（准则 F7）。未启用 rehype-raw：内联 HTML 不渲染。 */
import { type ReactElement, type ReactNode, memo, useEffect, useRef, useState } from 'react'
import ReactMarkdown, { type Components, type Options } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkBreaks from 'remark-breaks'
import rehypeHighlight from 'rehype-highlight'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { renderMermaidInto } from '@/lib/mermaid'
import { ensureCanvasBackground } from './svgNormalize'
import { SvgFigure } from './SvgFigure'
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

/** mermaid 块：顶栏（复制源码 + 下载 SVG） + 渲染图。SVG 序列化自渲染容器内的 <svg>。
    theme 入依赖：Light|Dark 切换后以对应 mermaid 主题重渲染。 */
function MermaidBlock({ code }: { code: string }): ReactNode {
  const ref = useRef<HTMLDivElement>(null)
  const [copied, setCopied] = useState(false)
  const theme = useThemeStore((s) => s.theme)
  useEffect(() => {
    let active = true
    void (async () => {
      await renderMermaidInto(ref.current, code)
      if (!active && ref.current) ref.current.innerHTML = ''
    })()
    return () => {
      active = false
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
      </div>
      <div ref={ref} className="md-mermaid__body" />
    </div>
  )
}

/** 图像：渲染 + 悬浮「下载」按钮（同源/data URL 直接下载；跨源需后端代理，故 best-effort）。 */
function MarkdownImage({ src, alt }: { src?: string; alt?: string }): ReactNode {
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
      <img src={src} alt={alt} />
      <button type="button" className="md-img__dl" onClick={onDownload} title="下载图像">
        下载
      </button>
    </span>
  )
}

/** breaks：单次换行渲染为 <br>（所见即所得）。用于用户气泡——非技术用户
    不会写"行尾两空格"或空行分段，GFM 默认会把多行合并成一段；
    同时保留加粗/列表/代码等轻量语法，懂 markdown 的用户不受影响。

    性能：插件表与组件映射提为模块常量、组件本身 memo——props 仅有
    （children: string, breaks），内容不变即完全跳过重渲染， remark→rehype
    （gfm/math/highlight/katex）全管线不重跑。这是流式期间"每 token 重解析
    全部历史消息"这一长会话卡顿主因的修复点之一。 */
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
  table: (props) => (
    <table {...props} style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0' }} />
  ),
  th: (props) => (
    <th {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px', background: 'var(--color-border-soft)' }} />
  ),
  td: (props) => <td {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px' }} />,
}

export const Markdown = memo(function Markdown({
  children,
  breaks = false,
}: {
  children: string
  breaks?: boolean
}): ReactNode {
  return (
    <div className="gd-md" style={{ lineHeight: 1.7, wordBreak: 'break-word' }}>
      <ReactMarkdown
        remarkPlugins={breaks ? REMARK_PLUGINS_BREAKS : REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={MD_COMPONENTS}
      >
        {normalizeMathDelimiters(children)}
      </ReactMarkdown>
    </div>
  )
})
