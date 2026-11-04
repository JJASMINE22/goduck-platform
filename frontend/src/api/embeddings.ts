/* 向量模型注册 API —— 对应后端 api/embeddings.py（prefix /embeddings）。
   CRUD + /test 连通探测（不落库）。api_key 列表接口永不返回。
   语义字段（端点/型号/维度）被知识库引用时后端 409 锁定（referencing_kbs）。 */
import { del, get, post, put } from './client'
import type {
  EmbeddingCreate,
  EmbeddingOut,
  EmbeddingTestRequest,
  EmbeddingTestResult,
  EmbeddingUpdate,
  UUID,
} from '@/types'

export const embeddingsApi = {
  list: () => get<EmbeddingOut[]>('/embeddings'),

  create: (body: EmbeddingCreate) => post<EmbeddingOut>('/embeddings', body),

  update: (id: UUID, body: EmbeddingUpdate) => put<EmbeddingOut>(`/embeddings/${id}`, body),

  /** 删除被引用向量模型 → 409 含 referencing_kbs（前端提示先换绑） */
  remove: (id: UUID) => del<{ ok: boolean }>(`/embeddings/${id}`),

  /** 保存前连通探测（embed 探针 → 实测维度），不落库 */
  test: (body: EmbeddingTestRequest) => post<EmbeddingTestResult>('/embeddings/test', body),
}
