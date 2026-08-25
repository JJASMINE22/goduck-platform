/* 知识库类型 —— 对应后端 models/schemas.py（KnowledgeBase 系列、KbDocumentOut、KbSearchResult）
   + api/knowledge.py（prefix /knowledge-bases）。
   - 仅收 pdf/docx（服务端魔数判定，前端只传 file）。
   - 上传仅注册（pending=待解析）；解析手动触发（POST .../parse，可带切块参数）。
   - 维度 per-KB：建库可设（默认全局）；有文档时修改须 force（文档重置为待解析，不删除）。 */
import type { ISODateTime, UUID } from './common'

/** 知识库状态（status 字段） */
export type KbStatus = 'active' | 'error'

/** 文档入库状态（status 字段，parse→ingest 流转；pending 为常驻「待解析」态） */
export type KbDocStatus = 'pending' | 'indexing' | 'indexed' | 'failed'

export interface KnowledgeBaseCreate {
  name: string
  description?: string | null
  creator: string
  /** 嵌入维度；缺省=后端全局 EMBEDDING_DIMS */
  dims?: number | null
}

export interface KnowledgeBaseUpdate {
  name: string
  description?: string | null
  /** 新维度；与当前不同时后端触发守卫（有文档须 force） */
  dims?: number | null
  /** 维度变更确认位：清空索引块 + 文档重置为待解析（原始文件保留，不删除） */
  force?: boolean
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
  progress?: number | null
  /** 解析切块参数（生效值，解析触发时写入；NULL=历史行未解析过） */
  chunk_size?: number | null
  chunk_overlap?: number | null
  error?: string | null
  created_at: ISODateTime
  updated_at: ISODateTime
}

/** POST .../documents/{id}/parse —— 手动解析触发参数（缺省沿用已存值/全局默认） */
export interface KbParseRequest {
  chunk_size?: number | null
  chunk_overlap?: number | null
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

/** GET /sessions/{id}/knowledge-capability —— 驱动会话框 KB 选择器可见性（准则 F2） */
export interface KnowledgeCapability {
  allow_knowledge: boolean
}
