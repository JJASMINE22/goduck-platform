/* 知识库类型 —— 对应后端 models/schemas.py（KnowledgeBase 系列、KbDocumentOut、KbSearchResult）
   + api/knowledge.py（prefix /knowledge-bases）。
   - 仅收 pdf/docx（服务端魔数判定，前端只传 file）。
   - 文档入库异步：status 在 pending→indexing→indexed|failed 间流转。 */
import type { ISODateTime, UUID } from './common'

/** 知识库状态（status 字段） */
export type KbStatus = 'active' | 'error'

/** 文档入库状态（status 字段，knowledge_service.ingest_document 流转） */
export type KbDocStatus = 'pending' | 'indexing' | 'indexed' | 'failed'

export interface KnowledgeBaseCreate {
  name: string
  description?: string | null
  creator: string
}

export interface KnowledgeBaseUpdate {
  name: string
  description?: string | null
}

export interface KnowledgeBaseOut {
  id: UUID
  name: string
  description?: string | null
  es_index_name: string
  dims?: number | null
  status: string
  doc_count: number
  chunk_count: number
  created_at: ISODateTime
  updated_at: ISODateTime
}

export interface KbDocumentOut {
  id: UUID
  kb_id: UUID
  filename: string
  mime: string
  size: number
  page_count?: number | null
  status: string
  chunk_count?: number | null
  error?: string | null
  created_at: ISODateTime
  updated_at: ISODateTime
}

/** GET /knowledge-bases/{id}/search 调试检索结果（管理面用） */
export interface KbSearchResult {
  content: string
  score: number
  filename: string
  doc_id: UUID
  page?: number | null
  chunk_index?: number | null
  kb_id?: UUID | null
}
