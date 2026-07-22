/* 附件 hooks —— 对应 api/uploads.ts。
   - useAttachmentCapability：驱动上传组件可见性（DESIGN 准则 F2 能力驱动 UI）
   - useUploadFile：multipart 上传，成功后由调用方存入 chatStore.attachments */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { uploadsApi } from '@/api/uploads'
import type { UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useAttachmentCapability(sessionId: UUID | null | undefined) {
  return useQuery({
    queryKey: queryKeys.attachmentCapability(sessionId ?? ''),
    queryFn: () => uploadsApi.capability(sessionId!),
    enabled: !!sessionId,
  })
}

export function useUploadFile(sessionId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ file, userId }: { file: File; userId: string }) =>
      uploadsApi.upload(sessionId, file, userId),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: queryKeys.attachmentCapability(sessionId) }),
  })
}
