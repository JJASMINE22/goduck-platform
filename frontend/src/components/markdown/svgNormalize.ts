/* svgNormalize —— 内联 SVG「色相保留归一化」+ 独立导出垫底。
   背景：svg-diagram 技能约定模型只写语义类，但技能由智能体自治触发，未调用时模型
   会按白底习惯手写硬编码色（深色文字 / 浅色填充），暗色主题下与画布同色不可读。
   策略（模型管色相，渲染器管明度）：
     · 文字（text/tspan/textPath）：剥离硬编码色 → 落回 .svg-scope text / t-* 令牌色；
     · 形状填充 / 渐变 stop-color：色相保留、明度锚定主题面板
       → color-mix(原色 14%, var(--color-bg-elevated))；
     · 描边与 marker 内实心箭头：明度锚定主题文字色
       → color-mix(原色 70%, var(--color-text))；
     · 无彩色（纯白/纯黑/灰系）：直接映射面板/边框/箭头令牌。
   已带语义类（清单与 svgScope.css、skills/svg-diagram 同步）的元素只剥离不重写——
   颜色归类系统所有。合规 SVG（零颜色）整趟零改动穿过。
   归一化只输出引用根令牌（tokens.css :root/[data-theme]）的表达式：与主题无关，
   切换主题由变量翻转完成，无需重跑。
   ensureCanvasBackground：独立导出的 SVG 脱离页面容器没有画布底，暗色主题下浅色
   文字在白底查看器不可见 → 垫一整幅当前主题面板色矩形（已有全幅底则跳过）。 */

/** svgScope.css 语义类清单（三方同步：svgScope.css / SKILL.md / 此处） */
const SCOPE_CLASSES = new Set([
  'c-frontend', 'c-backend', 'c-database', 'c-cloud', 'c-security', 'c-external', 'c-mask',
  't-primary', 't-muted', 't-dim',
  'a-default', 'a-emphasis', 'a-dashed',
  'm-default', 'm-emphasis',
])

const TEXT_TAGS = new Set(['text', 'tspan', 'textpath'])
const COLOR_PROPS = new Set(['fill', 'stroke', 'stop-color', 'color'])
const COLOR_ATTRS = ['fill', 'stroke', 'stop-color'] as const

/** 无彩色判定容差：RGB 极差 ≤ 16（约 6%）视为灰系（白背景板、黑字等） */
const ACHROMATIC_MAX_DELTA = 16
/** 完全透明的填充视为「无形板」：保留原样，避免透明占位被重写成可见面板色 */
const MIN_ALPHA = 0.02
const FILL_MIX = '14%'
const STROKE_MIX = '70%'
const SVG_NS = 'http://www.w3.org/2000/svg'

interface ElementCtx {
  /** 带语义类：颜色交给类系统，硬编码色只剥离 */
  classed: boolean
  /** 文字元素：一律落回文字令牌 */
  isText: boolean
  /** marker 内实心形（箭头三角）：按描边/箭头配方而非面板浅染 */
  inMarker: boolean
}

type Decl = { prop: string; value: string }
type ColorAction = { kind: 'keep' } | { kind: 'drop' } | { kind: 'set'; value: string }

const KEEP: ColorAction = { kind: 'keep' }
const DROP: ColorAction = { kind: 'drop' }

let colorProbe: CanvasRenderingContext2D | null | undefined

/** 探针画布（懒初始化）：借 fillStyle「写入-回读」把命名色/rgb()/hsl() 规范化为 #rrggbb / rgba() */
function probeCtx(): CanvasRenderingContext2D | null {
  if (colorProbe === undefined) {
    try {
      colorProbe = document.createElement('canvas').getContext('2d')
    } catch {
      colorProbe = null
    }
  }
  return colorProbe
}

/** 规范化为可嵌入 color-mix 的颜色串。返回 null = 不该/不能重写：
 *  none / transparent / currentColor / url() 引用 / 已是 var()/color-mix 表达式 / 非法值。 */
function parseColor(value: string): string | null {
  const v = value.trim().toLowerCase()
  if (!v || v === 'none' || v === 'transparent' || v === 'currentcolor' || v.startsWith('url(')) return null
  if (v.includes('var(') || v.includes('color-mix(')) return null
  const ctx = probeCtx()
  if (ctx) {
    ctx.fillStyle = '#123456' // 哨兵：写入失败时回读不变 → 非法颜色
    ctx.fillStyle = v
    return ctx.fillStyle !== '#123456' ? ctx.fillStyle : null
  }
  return /^#[0-9a-f]{3,8}$/.test(v) ? v : null
}

/** 解析为 [r, g, b, a]（0-255 / 0-1）；仅处理 parseColor 的规范化产物或 hex 兜底 */
function parseRgba(color: string): [number, number, number, number] | null {
  const v = color.trim().toLowerCase()
  const fn = /^rgba?\(([^)]+)\)$/.exec(v)
  if (fn) {
    const p = fn[1].split(/[\s,/]+/).filter(Boolean).map(Number)
    if (p.length < 3 || p.slice(0, 3).some((n) => !Number.isFinite(n))) return null
    const a = p.length >= 4 && Number.isFinite(p[3]) ? p[3] : 1
    return [p[0], p[1], p[2], a]
  }
  const hex = v.startsWith('#') ? v.slice(1) : ''
  const pair = (s: string): number => parseInt(s, 16)
  if (/^[0-9a-f]{3,4}$/.test(hex)) {
    const [r, g, b, a = 'f'] = hex.split('')
    return [pair(r + r), pair(g + g), pair(b + b), pair(a + a) / 255]
  }
  if (/^[0-9a-f]{6}$/.test(hex)) return [pair(hex.slice(0, 2)), pair(hex.slice(2, 4)), pair(hex.slice(4, 6)), 1]
  if (/^[0-9a-f]{8}$/.test(hex)) {
    return [pair(hex.slice(0, 2)), pair(hex.slice(2, 4)), pair(hex.slice(4, 6)), pair(hex.slice(6, 8)) / 255]
  }
  return null
}

function isAchromatic(rgba: [number, number, number, number]): boolean {
  const [r, g, b] = rgba
  return Math.max(r, g, b) - Math.min(r, g, b) <= ACHROMATIC_MAX_DELTA
}

/** 单个颜色声明的处置：keep=原样保留（none/引用/透明/非法），drop=剥离（文字/类接管），set=改写为主题自适应表达式 */
function actionFor(prop: string, rawValue: string, ctx: ElementCtx): ColorAction {
  const color = parseColor(rawValue)
  if (color === null) return KEEP
  const rgba = parseRgba(color)
  if (rgba === null || rgba[3] < MIN_ALPHA) return KEEP
  if (ctx.classed || ctx.isText) return DROP
  const gray = isAchromatic(rgba)
  if (prop === 'stroke') {
    return { kind: 'set', value: gray ? 'var(--color-border)' : `color-mix(in srgb, ${color} ${STROKE_MIX}, var(--color-text))` }
  }
  if (ctx.inMarker) {
    return { kind: 'set', value: gray ? 'var(--color-text-tertiary)' : `color-mix(in srgb, ${color} ${STROKE_MIX}, var(--color-text))` }
  }
  return { kind: 'set', value: gray ? 'var(--color-bg-elevated)' : `color-mix(in srgb, ${color} ${FILL_MIX}, var(--color-bg-elevated))` }
}

/** 拆解 style 属性为声明列表（无冒号的无效片段丢弃） */
function parseDecls(style: string | null): Decl[] {
  if (!style) return []
  const decls: Decl[] = []
  for (const chunk of style.split(';')) {
    const i = chunk.indexOf(':')
    if (i < 0) continue
    const prop = chunk.slice(0, i).trim().toLowerCase()
    const value = chunk.slice(i + 1).trim()
    if (prop && value) decls.push({ prop, value })
  }
  return decls
}

function upsertDecl(decls: Decl[], prop: string, value: string): void {
  const existing = decls.find((d) => d.prop === prop)
  if (existing) existing.value = value
  else decls.push({ prop, value })
}

/** 颜色属性（fill/stroke/stop-color）与 style 声明合并归一化，最后一次性写回 */
function normalizeElement(el: Element): void {
  const ctx: ElementCtx = {
    classed: Array.from(el.classList).some((c) => SCOPE_CLASSES.has(c)),
    isText: TEXT_TAGS.has(el.tagName.toLowerCase()),
    inMarker: el.closest('marker') !== null,
  }
  const decls = parseDecls(el.getAttribute('style'))
  for (const attr of COLOR_ATTRS) {
    const raw = el.getAttribute(attr)
    if (raw === null) continue
    const action = actionFor(attr, raw, ctx)
    if (action.kind === 'keep') continue
    el.removeAttribute(attr)
    if (action.kind === 'set') upsertDecl(decls, attr, action.value)
  }
  const out: Decl[] = []
  for (const d of decls) {
    if (!COLOR_PROPS.has(d.prop)) {
      out.push(d)
      continue
    }
    const action = actionFor(d.prop, d.value, ctx)
    if (action.kind === 'keep') out.push(d)
    else if (action.kind === 'set') out.push({ prop: d.prop, value: action.value })
  }
  const css = out.map((d) => `${d.prop}: ${d.value}`).join('; ')
  if (css) el.setAttribute('style', css)
  else el.removeAttribute('style')
}

/** 对净化后的 SVG 文档根做色相保留归一化（原地修改）。任何异常都不阻断渲染：退回原样。 */
export function normalizeSvgColors(svg: SVGSVGElement): void {
  try {
    for (const el of Array.from(svg.querySelectorAll('*'))) {
      normalizeElement(el)
    }
  } catch {
    // 归一化是增强兜底，失败时按未处理原文渲染
  }
}

/** 单个矩形是否覆盖全画布（起点 0,0；100% 或 ≥98% viewBox 尺寸） */
function coversCanvas(el: Element, vw: number, vh: number): boolean {
  const num = (attr: string): number => parseFloat(el.getAttribute(attr) ?? '')
  const x = num('x')
  const y = num('y')
  const w = num('width')
  const h = num('height')
  const atOrigin = (x === 0 || Number.isNaN(x)) && (y === 0 || Number.isNaN(y))
  const widthOk = (el.getAttribute('width') ?? '').trim() === '100%' || (Number.isFinite(w) && w >= vw * 0.98)
  const heightOk = (el.getAttribute('height') ?? '').trim() === '100%' || (Number.isFinite(h) && h >= vh * 0.98)
  return atOrigin && widthOk && heightOk
}

function hasCanvasRect(svg: SVGSVGElement): boolean {
  const parts = (svg.getAttribute('viewBox') ?? '').trim().split(/[\s,]+/)
  const vw = parts.length === 4 ? parseFloat(parts[2]) : Number.NaN
  const vh = parts.length === 4 ? parseFloat(parts[3]) : Number.NaN
  return Array.from(svg.querySelectorAll('rect')).some((el) => coversCanvas(el, vw, vh))
}

/** 独立导出用：垫整幅当前主题面板色画布底（无 viewBox 时按 100% 对齐视口）。已有全幅底则跳过。 */
export function ensureCanvasBackground(svg: SVGSVGElement): void {
  try {
    if (hasCanvasRect(svg)) return
    const panel = getComputedStyle(document.documentElement).getPropertyValue('--color-bg-elevated').trim()
    const rect = document.createElementNS(SVG_NS, 'rect')
    rect.setAttribute('width', '100%')
    rect.setAttribute('height', '100%')
    rect.setAttribute('fill', panel || '#ffffff')
    svg.insertBefore(rect, svg.firstChild)
  } catch {
    // 垫底失败不影响导出主体
  }
}
