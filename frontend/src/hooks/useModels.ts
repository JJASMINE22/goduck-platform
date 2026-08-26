/* 模型注册 hooks —— 对应 api/models.ts。
   CRUD 变更后失效 ["models"]；/test 不落库故不失效。
   删除被引用模型抛 ApiError(409, {message, referencing_agents})，透传给调用方展示。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { modelsApi } from '@/api/models'
import { toastSuccess } from '@/lib/antdStatic'
import type { ModelCreate, ModelTestRequest, ModelUpdate, UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useModels() {
  return useQuery({ queryKey: queryKeys.models, queryFn: () => modelsApi.list() })
}

/** 视觉下挂候选：已启用 + supports_vision=true + enable_thinking=false
 *  （GET /models?supports_vision=true&enable_thinking=false——描述任务须显式关思考）。
 *  key 为 ['models'] 前缀子键，模型 CRUD 失效 ["models"] 时自动连带失效。 */
export function useVisionModels() {
  return useQuery({
    queryKey: queryKeys.modelsVision,
    queryFn: () => modelsApi.list({ supports_vision: true, enable_thinking: false }),
  })
}

export function useCreateModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: ModelCreate) => modelsApi.create(body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.models })
      toastSuccess('模型已注册')
    },
  })
}

export function useUpdateModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: UUID; body: ModelUpdate }) => modelsApi.update(id, body),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.models })
      // 后端已 invalidate 全部 agent，前端 agent 列表下次拉取即跟随
      toastSuccess('模型已更新')
    },
  })
}

export function useDeleteModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: UUID) => modelsApi.remove(id),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.models })
      toastSuccess('模型已删除')
    },
  })
}

/** 连通探测（不落库，不失效缓存） */
export function useTestModel() {
  return useMutation({ mutationFn: (body: ModelTestRequest) => modelsApi.test(body) })
}
