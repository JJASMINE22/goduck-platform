/* 工作区 hooks —— 对应 api/workspace.ts。
   - 能力/树为面板级查询；文件/预览按选中路径按需取。
   - 树 staleTime:0：agent 写文件后刷新即取最新落盘拓扑。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { workspaceApi } from '@/api/workspace'
import { toastSuccess } from '@/lib/antdStatic'
import type { UUID } from '@/types'
import { queryKeys } from './queryKeys'

export function useWorkspaceCapability(
  sessionId: UUID | null | undefined,
  agentId?: UUID | null,
) {
  return useQuery({
    queryKey: queryKeys.workspaceCapability(sessionId ?? '', agentId ?? null),
    queryFn: () => workspaceApi.capability(sessionId!, agentId ?? null),
    enabled: !!sessionId,
  })
}

export function useWorkspaceTree(sessionId: UUID | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.workspaceTree(sessionId ?? ''),
    queryFn: () => workspaceApi.tree(sessionId!),
    enabled: !!sessionId && enabled,
    staleTime: 0,
  })
}

export function useWorkspaceFile(sessionId: UUID, path: string | null) {
  return useQuery({
    queryKey: queryKeys.workspaceFile(sessionId, path ?? ''),
    queryFn: () => workspaceApi.file(sessionId, path!),
    enabled: !!path,
    staleTime: 0,
  })
}

export function useWorkspacePreview(sessionId: UUID, path: string | null) {
  return useQuery({
    queryKey: queryKeys.workspacePreview(sessionId, path ?? ''),
    queryFn: () => workspaceApi.preview(sessionId, path!),
    enabled: !!path,
    staleTime: 0,
    // markitdown 转换可能 503（引擎未就绪）—— 不重试，组件降级为下载卡片。
    retry: 0,
  })
}

/** 删除文件或目录（递归删目录）—— 成功后失效树缓存触发重拉，并提示。 */
export function useDeleteWorkspaceItem(sessionId: UUID) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (path: string) => workspaceApi.delete(sessionId, path),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: queryKeys.workspaceTree(sessionId) })
      toastSuccess(`已删除${data.was_dir ? '目录' : '文件'} ${data.name}`)
    },
  })
}
