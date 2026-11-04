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
  /** 绑定向量模型（优先；维度随行）。缺省=不绑定，走全局 env 维度（迁移期语义） */
  embedding_id?: UUID | null
  /** legacy 维度入参（未绑定向量模型时生效）；缺省=后端全局 EMBEDDING_DIMS */
  dims?: number | null
}

export interface KnowledgeBaseUpdate {
  name: string
  description?: string | null
  /** 换绑向量模型（不传=不换）。换绑与维度变更同为破坏性变更（有文档须 force） */
  embedding_id?: UUID | null
  dims?: number | null
  /** 换绑/维度变更确认位：清空索引块 + 文档重置为待解析（原始文件保留） */
  force?: boolean
}

export interface KnowledgeBaseOut {
  id: UUID
  name: string
  description?: string | null
  es_index_name: string
  dims?: number | null
  /** 绑定向量模型（null=legacy：dims + env 双读回落） */
  embedding_id?: UUID | null
  embedding_name?: string | null
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
  /** 解析切块参数（生效值，解析触发时写入；NULL=历史行未解析过；docling 路径忽略） */
  chunk_size?: number | null
  chunk_overlap?: number | null
  /** kb-src 归档版本（当前恒 1） */
  doc_version?: number | null
  /** 产出当前 chunk 的引擎（'legacy'|'docling'；NULL=legacy/历史行） */
  parser_engine?: string | null
  error?: string | null
  created_at: ISODateTime
  updated_at: ISODateTime
}

/** POST .../documents/{id}/parse —— 手动解析触发参数（缺省沿用已存值/全局默认） */
export interface KbParseRequest {
  chunk_size?: number | null
  chunk_overlap?: number | null
}

/** GET /knowledge-bases/parser-engine —— 解析引擎状态（C1 双轨语义驱动） */
export interface KbParserEngine {
  engine: 'legacy' | 'docling'
  /** 结构单元预算上界（仅 docling 路径生效） */
  unit_max_chars: number
}

/** 归一化锚框（左上原点 0..1；B2 契约：可空 = 文本态降级） */
export interface KbBbox {
  x0: number
  y0: number
  x1: number
  y1: number
}

/** 命中绑定的图片资产（图-图注绑定；thumb 仅供列表预览） */
export interface KbHitAsset {
  kind: 'picture'
  page?: number | null
  bbox?: KbBbox | null
  caption?: string | null
  thumb_b64?: string | null
}

/** GET /knowledge-bases/{id}/search 调试检索结果（管理面用）。
 *  docling 证据锚定字段全可空——legacy / docx 直解 / 归档失败兜底同轨 null
 *  （存在即锚定、缺失即文本态降级，B2 可空契约）。 */
export interface KbSearchResult {
  content: string
  score: number
  filename: string
  doc_id: UUID
  page?: number | null
  chunk_index?: number | null
  kb_id?: UUID | null
  kind?: string | null
  heading_path?: string[] | null
  page_rotate?: number | null
  /** 证据源 MinIO 键（kb-src/...） */
  doc_uri?: string | null
  /** doc_uri 预签名 URL（viewer 消费；过期需重新检索获取） */
  doc_url?: string | null
  bbox?: KbBbox | null
  assets?: KbHitAsset[] | null
}

/** POST /knowledge-bases/anchor-cards 取回键组（= kb_search 工具结果 hits 的定位键） */
export interface KbAnchorCardItem {
  kb_id: UUID
  doc_id: UUID
  chunk_index: number
}

/** 单命中锚定卡（聊天侧 kb_search 工具卡懒取；可空字段语义同 KbSearchResult） */
export interface KbAnchorCard {
  content: string
  filename: string
  heading_path?: string[] | null
  page?: number | null
  page_rotate?: number | null
  kind?: string | null
  doc_uri?: string | null
  doc_url?: string | null
  bbox?: KbBbox | null
  assets?: KbHitAsset[] | null
}

/** mcp kb_search 工具结果的载荷形状（mcp-servers/routers/knowledge.py 契约） */
export interface KbSearchToolPayload {
  result: 'ok' | 'error'
  query?: string
  total?: number
  message?: string
  hits?: Array<{
    content: string
    score: number
    filename: string
    doc_id: UUID | null
    page?: number | null
    chunk_index?: number | null
    kb_id?: UUID | null
  }>
}

/** GET /sessions/{id}/knowledge-capability —— 驱动会话框 KB 选择器可见性（准则 F2） */
export interface KnowledgeCapability {
  allow_knowledge: boolean
}
