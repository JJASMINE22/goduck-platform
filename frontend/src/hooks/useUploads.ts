/* 附件 hooks —— 对应 api/uploads.ts。
   - useAttachmentCapability：驱动上传组件可见性（DESIGN 准则 F2 能力驱动 UI）
   - useUploadFile：单文件 multipart 上传（并发调用时 hook 态不可靠，逐调用 Promise 准确）
   - useAttachFiles：选择器/粘贴共用的上传编排 —— 入队即渲染 pending chip，
     逐文件独立调用单文件接口（并发，后端契约不变），失败互不影响（错误由
     QueryClient mutations.onError 全局提示，保留后端中文 detail） */
import { useCallback } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { uploadsApi } from '@/api/uploads'
import { useChatStore, type PendingAttachment } from '@/store/chatStore'
import { toastError } from '@/lib/antdStatic'
import type { UUID } from '@/types'
import { queryKeys } from './queryKeys'

/** 附件能力（能力驱动 UI）：按当前选中智能体派生（§4.4 弱绑定解析，
 *  query key 含 agentId——切换选中即 keyed refetch）。 */
export function useAttachmentCapability(
  sessionId: UUID | null | undefined,
  agentId?: UUID | null,
) {
  return useQuery({
    queryKey: queryKeys.attachmentCapability(sessionId ?? '', agentId ?? null),
    queryFn: () => uploadsApi.capability(sessionId!, agentId ?? null),
    enabled: !!sessionId,
  })
}

export function useUploadFile(sessionId: UUID, agentId?: UUID | null) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ file, userId }: { file: File; userId: string }) =>
      uploadsApi.upload(sessionId, file, userId, agentId ?? null),
    onSuccess: () =>
      qc.invalidateQueries({
        queryKey: queryKeys.attachmentCapability(sessionId, agentId ?? null),
      }),
  })
}

// —— 编排辅助（模块级）：localId 序号 + 剪贴板泛化名重写 ——

let seq = 0
let lastShotBase = ''
let lastShotCount = 0

const pad = (n: number) => String(n).padStart(2, '0')

/** 展示名重写：剪贴板图片的泛化名（image.png / 无名）→ 截图-YYYYMMDD-HHmmss，
    同秒多张自增 -2/-3。仅影响展示与入库 filename，不参与服务端类型判定（魔数为准）。 */
function friendlyName(file: File): string {
  const name = file.name?.trim()
  if (name && name.toLowerCase() !== 'image.png') return name
  const d = new Date()
  const ext = file.type.split('/')[1] || 'png'
  const base = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
  lastShotCount = base === lastShotBase ? lastShotCount + 1 : 1
  lastShotBase = base
  return `截图-${base}${lastShotCount > 1 ? `-${lastShotCount}` : ''}.${ext}`
}

/** 多入口（选择器/粘贴）共用的附件上传编排。
     返回 attach（批量入队 + 并发上传）与 retry（按 localId 重试错误占位）。
     agentId=当前选中智能体（上传门能力谓词按它派生）。 */
export function useAttachFiles(sessionId: UUID, userId: string, agentId?: UUID | null) {
  const cap = useAttachmentCapability(sessionId, agentId)
  const { mutateAsync } = useUploadFile(sessionId, agentId)

  const run = useCallback(
    async (file: File, localId: string) => {
      try {
        const res = await mutateAsync({ file, userId })
        useChatStore.getState().resolvePending(localId, res)
      } catch {
        // 错误详情已由 mutations.onError 全局提示；占位转错误态留驻供重试
        useChatStore.getState().failPending(localId)
      }
    },
    [mutateAsync, userId],
  )

  const attach = useCallback(
    (files: File[]) => {
      const capData = cap.data
      if (!capData?.allow_attachments) return
      let skippedImage = 0
      let skippedType = 0
      for (const original of files) {
        // 能力预检（与选择器 accept 同语义；粘贴路径无 accept 可依，须在此挡）：
        // 图像需 allow_image；文档扩展名需在 allow_file。服务端 415/422 仍为最终门。
        if (original.type.startsWith('image/')) {
          if (!capData.allow_image) {
            skippedImage++
            continue
          }
        } else {
          const ext = original.name.includes('.')
            ? original.name.split('.').pop()!.toLowerCase()
            : ''
          if (!capData.allow_file.includes(ext)) {
            skippedType++
            continue
          }
        }
        // 大小预检（与既有选择器路径同源）
        if (original.size > capData.max_size_mb * 1024 * 1024) {
          toastError(`文件超过 ${capData.max_size_mb}MB 限制`)
          continue
        }
        const filename = friendlyName(original)
        // 重写过的名字需随请求下发（uploadsApi 以 file.name 为 filename）：
        // new File 包装原 blob 引用，零字节拷贝
        const file =
          filename === original.name ? original : new File([original], filename, { type: original.type })
        const kindHint = file.type.startsWith('image/') ? 'image' : 'file'
        const pending: PendingAttachment = {
          localId: `p${Date.now()}-${++seq}`,
          file,
          filename,
          size: file.size,
          kindHint,
          previewUrl: kindHint === 'image' ? URL.createObjectURL(file) : undefined,
          status: 'uploading',
        }
        useChatStore.getState().addPending(pending)
        void run(file, pending.localId)
      }
      if (skippedImage) {
        toastError('该智能体不支持图像附件（基座模型无视觉能力且未下挂视觉模型）')
      }
      if (skippedType) {
        toastError(`已跳过 ${skippedType} 个不支持的文件类型（支持 ${capData.allow_file.join('/')}）`)
      }
    },
    [cap.data, run],
  )

  const retry = useCallback(
    (localId: string) => {
      const p = useChatStore.getState().pendingAttachments.find((x) => x.localId === localId)
      if (!p || p.status !== 'error') return
      useChatStore.getState().setPendingUploading(localId)
      void run(p.file, localId)
    },
    [run],
  )

  return { attach, retry }
}
