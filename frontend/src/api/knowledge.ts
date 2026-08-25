/* 知识库 API —— 对应后端 api/knowledge.py（prefix /knowledge-bases）。
   - KB CRUD（建/改可带 dims）+ 文档（注册/手动解析/列表/删除/重建索引）+ 检索调试。
   - kb_enabled=false 时后端路由未挂载，接口 404（前端按全局能力开关门控，见 useCapabilities）。
   - 上传仅注册不解析；仅收 pdf/docx，前端只传 file（mime 服务端魔数判定，准则 F6）。 */
import { del, get, post, postForm, put } from './client'
import type {
  KbDocumentOut,
  KbParseRequest,
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

  /** multipart 上传 → 仅注册（pending=待解析）；解析由 parseDocument 手动触发 */
  uploadDocument: (kbId: UUID, file: File) => {
    const form = new FormData()
    form.append('file', file, file.name) // 保留 filename（docx 扩展名判定需要）
    return postForm<KbDocumentOut>(`/knowledge-bases/${kbId}/documents`, form)
  },

  /** 手动解析：持久化切块参数 + 后台入库（幂等，重新解析同样调用） */
  parseDocument: (kbId: UUID, docId: UUID, body?: KbParseRequest) =>
    post<KbDocumentOut>(`/knowledge-bases/${kbId}/documents/${docId}/parse`, body ?? {}),

  removeDocument: (kbId: UUID, docId: UUID) =>
    del<{ ok: boolean }>(`/knowledge-bases/${kbId}/documents/${docId}`),

  // —— 检索调试 ——
  search: (kbId: UUID, q: string, k = 5) =>
    get<KbSearchResult[]>(`/knowledge-bases/${kbId}/search`, { q, k }),
}
