/* 模型注册 API —— 对应后端 api/models.py（prefix /models）。
   CRUD + /test 连通探测（不落库，避免存入误配模型）。
   api_key 列表接口永不返回。 */
import { del, get, post, put } from './client'
import type { ModelCreate, ModelOut, ModelTestRequest, ModelTestResult, ModelUpdate, UUID } from '@/types'

export const modelsApi = {
  list: () => get<ModelOut[]>('/models'),

  create: (body: ModelCreate) => post<ModelOut>('/models', body),

  update: (id: UUID, body: ModelUpdate) => put<ModelOut>(`/models/${id}`, body),

  /** 删除被引用模型 → 409 含 referencing_agents（前端提示先解绑） */
  remove: (id: UUID) => del<{ ok: boolean }>(`/models/${id}`),

  /** 保存前连通探测，不落库 */
  test: (body: ModelTestRequest) => post<ModelTestResult>('/models/test', body),
}
