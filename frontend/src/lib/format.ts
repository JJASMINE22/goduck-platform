/* 格式化工具 —— 时间 / 字节 / uuid 短显。纯函数，无副作用。 */
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/** 完整日期时间：2026-07-21 10:05:30 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return dayjs(iso).format('YYYY-MM-DD HH:mm:ss')
}

/** 相对时间：3 分钟前 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  return dayjs(iso).fromNow()
}

/** 字节 → 人类可读：1.2 MB */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let n = bytes
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

/** uuid 取前 8 位（列表/日志展示用） */
export function shortId(uuid: string | null | undefined): string {
  if (!uuid) return '—'
  return uuid.slice(0, 8)
}

/** 提取文本展示清洗：去除 C0 控制符与 DEL（保留 \n \t；U+001A 为 docling
 *  对无 Unicode 映射字形的替换输出，浏览器渲染为可见乱符号）。
 *  仅作用于展示面，ES 真值不动。 */
export function sanitizeKbText(text: string | null | undefined): string {
  if (!text) return ''
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '')
}
