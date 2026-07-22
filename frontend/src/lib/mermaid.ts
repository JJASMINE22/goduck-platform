/* Mermaid 渲染封装（DESIGN 准则 F7：单一渲染入口，只渲染后端产出的文本）。
   mermaid v11 + securityLevel:strict。零图像/网络依赖，纯前端渲染拓扑文本。 */
import mermaid from 'mermaid'

let initialized = false

function ensureInit() {
  if (initialized) return
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict', // 禁止 mermaid 里的交互脚本/html
    theme: 'default',
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis' },
  })
  initialized = true
}

let seq = 0

/** 把 mermaid 文本渲染成 svg 字符串。空/非法文本返回空串。 */
export async function renderMermaid(code: string): Promise<string> {
  const trimmed = code?.trim()
  if (!trimmed) return ''
  ensureInit()
  // 唯一 id 避免多次渲染冲突
  const id = `gd-mermaid-${Date.now()}-${seq++}`
  try {
    const { svg } = await mermaid.render(id, trimmed)
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
