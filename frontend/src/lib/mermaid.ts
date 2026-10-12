/* Mermaid 渲染封装（DESIGN 准则 F7：单一渲染入口，只渲染后端的文本）。
    mermaid v11 + securityLevel:strict。零图像/网络依赖，纯前端渲染拓扑文本。
    主题感知：读取 <html data-theme>（themeStore 挂载，lib 不依赖 store），
    亮色注入 neo 主题抬头、暗色注入 dark 主题（避免暗图配亮气泡的风格不一致）；
    主题变更时重新 initialize，消费方（Markdown/MermaidPreview）以 theme 为
    effect 依赖触发重渲染。 */
import mermaid from 'mermaid'

type MermaidMode = 'light' | 'dark'

let initializedFor: MermaidMode | null = null

function currentMode(): MermaidMode {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'
}

function ensureInit(mode: MermaidMode) {
  if (initializedFor === mode) return
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict', // 禁止 mermaid 里的交互脚本/html
    theme: mode === 'dark' ? 'dark' : 'default', // 亮暗基底；流程图细节由抬头覆盖
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis' },
  })
  initializedFor = mode
}

/** neo（亮）/ dark（暗）主题 + elk 布局抬头。仅当代码本身未带 frontmatter 时注入，避免冲突。 */
function styleFrontmatter(mode: MermaidMode): string {
  return `---\nconfig:\n  theme: ${mode === 'dark' ? 'dark' : 'neo'}\n  layout: elk\n---\n`
}

let seq = 0

/** 把 mermaid 文本渲染成 svg 字符串。空/非法文本返回空串。 */
export async function renderMermaid(code: string): Promise<string> {
  const trimmed = code?.trim()
  if (!trimmed) return ''
  const mode = currentMode()
  ensureInit(mode)
  // 已带 frontmatter（以 --- 开头）则原样使用，避免重复抬头导致解析失败
  const source = /^---\s*\n/.test(trimmed) ? trimmed : styleFrontmatter(mode) + trimmed
  // 唯一 id 避免多次渲染冲突
  const id = `gd-mermaid-${Date.now()}-${seq++}`
  try {
    const { svg } = await mermaid.render(id, source)
    return svg
  } catch {
    // 非法文本（理论不应发生，文本来自后端）→ 返回原文兜底
    return ''
  }
}

/** 直接渲染到容器元素的 innerHTML */
export async function renderMermaidInto(el: HTMLElement | null, code: string): Promise<void> {
  if (!el) return
  const svg = await renderMermaid(code)
  el.innerHTML = svg || '<span style="color:var(--color-text-tertiary)">拓扑图为空</span>'
}
