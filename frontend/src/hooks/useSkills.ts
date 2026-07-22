/* 技能 hooks —— 对应 api/skills.ts。上传/删除后失效 ["skills"]。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { skillsApi } from '@/api/skills'
import { toastSuccess } from '@/lib/antdStatic'
import { queryKeys } from './queryKeys'

export function useSkills() {
  return useQuery({ queryKey: queryKeys.skills, queryFn: skillsApi.list })
}

export function useUploadSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, files }: { name: string; files: File[] }) =>
      skillsApi.upload(name, files),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.skills })
      toastSuccess('技能已上传')
    },
  })
}

export function useDeleteSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => skillsApi.remove(name),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.skills })
      toastSuccess('技能已删除')
    },
  })
}
