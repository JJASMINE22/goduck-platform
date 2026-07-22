/* 模型注册类型 —— 对应后端 models/schemas.py (ModelCreate/ModelUpdate/ModelOut/
   ModelTestRequest) + api/models.py。
   provider_kind 当前仅 openai_compatible（通吃 OpenAI/DeepSeek/Qwen/本地）。
   supports_vision 决定附件「图像直送 vs wrapper MCP」路径。 */
import type { ISODateTime, UUID } from './common'

export type ProviderKind = 'openai_compatible'

export interface ModelCreate {
  name: string
  provider_kind: ProviderKind
  model_type: string // 如 "gpt-4o-mini"
  api_key: string
  base_url?: string | null // OpenAI 兼容端点；原生 OpenAI 留空
  supports_vision: boolean
  enabled: boolean
}

export interface ModelUpdate {
  name: string
  provider_kind: ProviderKind
  model_type: string
  api_key?: string | null // null/undefined = 保留旧 key；非空则覆盖（轮换密钥）
  base_url?: string | null
  supports_vision: boolean
  enabled: boolean
}

export interface ModelOut {
  id: UUID
  name: string
  provider_kind: string
  model_type: string
  base_url?: string | null
  supports_vision: boolean
  enabled: boolean
  created_at: ISODateTime
  updated_at: ISODateTime
}

export interface ModelTestRequest {
  provider_kind: ProviderKind
  model_type: string
  api_key: string
  base_url?: string | null
}

export interface ModelTestResult {
  ok: boolean
  model_type?: string
  error?: string | null
}

/** 删除被引用模型时后端 409 detail（api/models.py delete_model） */
export interface ModelInUseDetail {
  message: string
  referencing_agents: Array<{ id: string; name: string }>
}
