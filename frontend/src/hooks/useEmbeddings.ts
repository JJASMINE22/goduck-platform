/* 向量模型注册 hooks —— 对应 api/embeddings.ts。
   CRUD 变更后失效 ["embeddings"]（建库表单下拉随之刷新）；/test 不落库不失效。
   语义变更/删除被引用抛 ApiError(409, {message, referencing_kbs})，透传给调用方。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { embeddingsApi } from '@/api/embeddings'
import { toastSuccess } from '@/lib/antdStatic'
import type { EmbeddingCreate, EmbeddingTestRequest, EmbeddingUpdate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useEmbeddings() {
  return useQuery({ queryKey: queryKeys.embeddings, queryFn: embeddingsApi.list })
}

export function useCreateEmbedding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: EmbeddingCreate) => embeddingsApi.create(body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.embeddings })
      toastSuccess('向量模型已注册')
    },
  })
}

export function useUpdateEmbedding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: EmbeddingUpdate }) =>
      embeddingsApi.update(id, body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.embeddings })
      toastSuccess('向量模型已更新')
    },
  })
}

export function useDeleteEmbedding() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => embeddingsApi.remove(id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.embeddings })
      toastSuccess('向量模型已删除')
    },
  })
}

/** 连通探测（不落库，不失效缓存） */
export function useTestEmbedding() {
  return useMutation({ mutationFn: (body: EmbeddingTestRequest) => embeddingsApi.test(body) })
}
