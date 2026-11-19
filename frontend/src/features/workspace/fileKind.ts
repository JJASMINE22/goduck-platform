/* 工作区文件预览分类 —— 按扩展名决定前端预览分派策略（plan §3.2-3）。
   分类仅驱动"怎么取/怎么渲染"；能否预览的最终裁决在后端（/file 返回 is_text、
   /preview 返回 markdown 或 503/415）。任何预览失败都回退到降级卡片（不留空白）。
   html/htm 单列：/file 取文本 → 前端沙箱 iframe 渲染（渲染/源码切换）。
   svg 单列：/file 取文本 → SvgFigure 净化 + 主题化渲染。平台产出的 SVG 按
   svg-diagram 技能约定不写内联色、只用语义类，<img> 隔离文档里语义类无定义，
   全部形状落回默认黑填充（深浅主题下都是黑块）——必须走页面内作用域渲染。 */

export type WorkspaceFileKind = 'text' | 'markdown' | 'image' | 'pdf' | 'office' | 'html' | 'svg' | 'binary'

const TEXT_EXTS = new Set([
  'txt', 'log', 'ini', 'cfg', 'conf', 'toml', 'env',
  'json', 'yaml', 'yml', 'xml', 'csv', 'tsv',
  'py', 'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs',
  'go', 'rs', 'java', 'kt', 'c', 'h', 'cpp', 'hpp', 'cc', 'cs', 'rb', 'php',
  'sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd',
  'sql', 'css', 'scss', 'less', 'vue', 'svelte',
  'dart', 'swift', 'scala', 'clj', 'lua', 'r', 'pl',
  'dockerfile', 'gitignore', 'makefile',
])

const MARKDOWN_EXTS = new Set(['md', 'markdown', 'mdx'])

// html/htm 单列：走沙箱 iframe 渲染（渲染/源码可切换），不作为普通文本。
const HTML_EXTS = new Set(['html', 'htm'])

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico'])

// markitdown 可转换的 office/文档类型（与后端 CONVERTIBLE_EXTS 对齐）。
const OFFICE_EXTS = new Set(['docx', 'xlsx', 'pptx', 'pdf', 'doc', 'xls', 'ppt', 'csv'])

/** 文件名里的扩展名小写（无点）。Dockerfile/Makefile 等无后缀全名文件按名识别。 */
function extOf(filename: string): string {
  const lower = filename.toLowerCase()
  if (['dockerfile', 'makefile', '.gitignore'].some((n) => lower === n || lower.endsWith('/' + n))) {
    return lower.split('/').pop() ?? ''
  }
  const idx = lower.lastIndexOf('.')
  return idx >= 0 ? lower.slice(idx + 1) : ''
}

export function classifyFile(filename: string): WorkspaceFileKind {
  const ext = extOf(filename)
  if (MARKDOWN_EXTS.has(ext)) return 'markdown'
  // pdf 单列：浏览器原生 iframe 渲染最佳，不走 markitdown。
  if (ext === 'pdf') return 'pdf'
  if (HTML_EXTS.has(ext)) return 'html'
  // svg 单列：语义类主题化渲染（见文件头注释），不作为位图 <img>。
  if (ext === 'svg') return 'svg'
  if (IMAGE_EXTS.has(ext)) return 'image'
  // 其余 office 类型（docx/xlsx/pptx/csv 等）走 markitdown 预览。
  if (OFFICE_EXTS.has(ext)) return 'office'
  if (TEXT_EXTS.has(ext)) return 'text'
  return 'binary'
}

/** 人类可读体积。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
