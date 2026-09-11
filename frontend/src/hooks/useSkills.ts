/* 技能 hooks —— 对应 api/skills.ts。上传/删除后失效 ["skills"]。 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { skillsApi } from '@/api/skills'
import { toastSuccess } from '@/lib/antdStatic'
import type { FlatFileEntry } from '@/lib/flattenFiles'
import { queryKeys } from './queryKeys'

export function useSkills() {
  return useQuery({ queryKey: queryKeys.skills, queryFn: skillsApi.list })
}

/** 技能描述（SKILL.md frontmatter description）；与技能列表共享失效键前缀 */
export function useSkillDescription(name: string) {
  return useQuery({
    queryKey: queryKeys.skillDescription(name),
    queryFn: () => skillsApi.description(name),
    enabled: !!name,
  })
}

export function useUploadSkill() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, files }: { name: string; files: FlatFileEntry[] }) =>
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
