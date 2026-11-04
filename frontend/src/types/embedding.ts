/* 向量模型注册类型 —— 对应后端 models/schemas.py (EmbeddingCreate/EmbeddingUpdate/
   EmbeddingOut/EmbeddingTestRequest) + api/embeddings.py。
   注册/语义变更时后端经端点实证维度（声明≠实测即 400）；
   语义字段被知识库引用时锁定不可变（409 referencing_kbs，先换绑再改）。 */
import type { ISODateTime, UUID } from './common'

export type EmbeddingProviderKind = 'openai_compatible'

export interface EmbeddingCreate {
  name: string
  provider_kind: EmbeddingProviderKind
  model_type: string // TEI 部署名（TEI 忽略）；云端须真实型号
  api_key: string // TEI 用任意非空值（dummy）
  base_url: string // OpenAI 兼容端点（含 /v1；compose 内 TEI=http://tei:8082/v1）
  /** MRL 截断维度；null=模型原生。注册时经端点实证 */
  dims?: number | null
  batch_size: number // 单请求文本数（≤ TEI --max-client-batch-size）
  timeout: number // 单批请求超时（秒）
  check_ctx_length: boolean // TEI 必须 false（tiktoken 规避）；真实 OpenAI 设 true
  enabled: boolean
}

export interface EmbeddingUpdate {
  name: string
  provider_kind: EmbeddingProviderKind
  model_type: string
  api_key?: string | null // null/undefined = 保留旧 key；非空覆盖
  base_url: string
  dims?: number | null
  batch_size: number
  timeout: number
  check_ctx_length: boolean
  enabled: boolean
}

export interface EmbeddingOut {
  id: UUID
  name: string
  provider_kind: string
  model_type: string
  base_url: string
  dims?: number | null
  batch_size: number
  timeout: number
  check_ctx_length: boolean
  enabled: boolean
  created_at: ISODateTime
  updated_at: ISODateTime
}

export interface EmbeddingTestRequest {
  model_type: string
  api_key: string
  base_url: string
  timeout?: number
  check_ctx_length?: boolean
}

/** 保存前连通探测（不落库）：embed 一条探针 → 实测维度 */
export interface EmbeddingTestResult {
  ok: boolean
  dims?: number
  error?: string | null
}

/** 语义变更/删除被引用时后端 409 detail（api/embeddings.py） */
export interface EmbeddingReferencedDetail {
  message: string
  referencing_kbs: Array<{ id: string; name: string }>
}
