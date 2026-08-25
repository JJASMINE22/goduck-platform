/* 文档状态标签 —— 统一 pending/indexing/indexed/failed 的配色与文案。
   解析手动触发（parse→ingest）后的状态流转映射；indexing 时渲染进度条。
   pending 为常驻「待解析」态（上传仅注册，等待用户手动解析）。 */
import { Progress, Tag } from 'antd'
import { type ReactNode } from 'react'
import type { KbDocStatus } from '@/types'

const MAP: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '待解析' },
  indexing: { color: 'processing', text: '解析中' },
  indexed: { color: 'success', text: '已解析' },
  failed: { color: 'error', text: '失败' },
}

function clampPct(v?: number | null): number {
  if (v == null || Number.isNaN(v)) return 0
  return Math.max(0, Math.min(100, Math.round(v)))
}

export function KbDocStatusTag({
  status,
  progress,
}: {
  status: string
  progress?: number | null
}): ReactNode {
  // indexing：渲染进度条（progress 由后端逐批回写 0..100；轮询驱动更新）
  if (status === 'indexing') {
    return <Progress percent={clampPct(progress)} size="small" />
  }
  const cfg = MAP[status as KbDocStatus] ?? { color: 'default', text: status }
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}
