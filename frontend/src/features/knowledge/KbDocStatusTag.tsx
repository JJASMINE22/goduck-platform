/* 文档入库状态标签 —— 统一 pending/indexing/indexed/failed 的配色与文案。
   knowledge_service.ingest_document 的状态流转映射；indexing 时渲染进度条。 */
import { Progress, Tag } from 'antd'
import { type ReactNode } from 'react'
import type { KbDocStatus } from '@/types'

const MAP: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '待入库' },
  indexing: { color: 'processing', text: '入库中' },
  indexed: { color: 'success', text: '已入库' },
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
