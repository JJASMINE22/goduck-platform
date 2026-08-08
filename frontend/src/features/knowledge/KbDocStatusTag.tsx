/* 文档入库状态标签 —— 统一 pending/indexing/indexed/failed 的配色与文案。
   knowledge_service.ingest_document 的状态流转映射。 */
import { Tag } from 'antd'
import { type ReactNode } from 'react'
import type { KbDocStatus } from '@/types'

const MAP: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '待入库' },
  indexing: { color: 'processing', text: '入库中' },
  indexed: { color: 'success', text: '已入库' },
  failed: { color: 'error', text: '失败' },
}

export function KbDocStatusTag({ status }: { status: string }): ReactNode {
  const cfg = MAP[status as KbDocStatus] ?? { color: 'default', text: status }
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}
