/* 知识库 API —— 对应后端 api/knowledge.py（prefix /knowledge-bases）。
   - KB CRUD + 文档（注册/列表/删除/重建索引）+ 管理面检索调试。
   - kb_enabled=false 时后端路由未挂载，接口 404（前端按全局能力开关门控，见 useCapabilities）。
   - 文档上传仅收 pdf/docx，前端只传 file（mime 服务端魔数判定，准则 F6）。 */
import { del, get, post, postForm, put } from './client'
import type {
  KbDocumentOut,
  KbSearchResult,
  KnowledgeBaseCreate,
  KnowledgeBaseOut,
  KnowledgeBaseUpdate,
  UUID,
} from '@/types'

export const knowledgeApi = {
  // —— KB CRUD ——
  list: () => get<KnowledgeBaseOut[]>('/knowledge-bases'),

  create: (body: KnowledgeBaseCreate) => post<KnowledgeBaseOut>('/knowledge-bases', body),

  update: (id: UUID, body: KnowledgeBaseUpdate) =>
    put<KnowledgeBaseOut>(`/knowledge-bases/${id}`, body),

  remove: (id: UUID) => del<{ ok: boolean }>(`/knowledge-bases/${id}`),

  // —— 文档 ——
  listDocuments: (kbId: UUID) => get<KbDocumentOut[]>(`/knowledge-bases/${kbId}/documents`),

  /** multipart 上传 → 注册 + 后台入库（返回 pending 状态卡） */
  uploadDocument: (kbId: UUID, file: File) => {
    const form = new FormData()
    form.append('file', file, file.name) // 保留 filename（docx 扩展名判定需要）
    return postForm<KbDocumentOut>(`/knowledge-bases/${kbId}/documents`, form)
  },

  removeDocument: (kbId: UUID, docId: UUID) =>
    del<{ ok: boolean }>(`/knowledge-bases/${kbId}/documents/${docId}`),

  /** 重建索引（需 MinIO 留底）：删旧 chunk 后重新入库 */
  reindexDocument: (kbId: UUID, docId: UUID) =>
    post<KbDocumentOut>(`/knowledge-bases/${kbId}/documents/${docId}/reindex`),

  // —— 检索调试 ——
  search: (kbId: UUID, q: string, k = 5) =>
    get<KbSearchResult[]>(`/knowledge-bases/${kbId}/search`, { q, k }),
}
