/* 技能 API —— 对应后端 api/skills.py（prefix /skills）。
   全局 skills 池共享，一个 skill = 一个子目录。multipart 上传防目录穿越，
   支持目录结构：filename 用相对路径（scripts/run.js），后端重建子目录。 */
import { del, get, postForm } from './client'
import type { FlatFileEntry } from '@/lib/flattenFiles'
import type { SkillDetail, SkillInfo } from '@/types'

export const skillsApi = {
  /** 列出技能（子目录 + 文件数） */
  list: () => get<SkillInfo[]>('/skills'),

  /** 技能描述：解析 SKILL.md（缺失回退 README.md）frontmatter 的 description */
  description: (name: string) => get<SkillDetail>(`/skills/${name}/description`),

  /** multipart 上传：name + 多文件 → 写入 {pool}/<name>/；path 携带相对路径以重建子目录 */
  upload: (name: string, files: FlatFileEntry[]) => {
    const form = new FormData()
    form.append('name', name)
    for (const { file, path } of files) {
      form.append('files', file, path || file.name)
    }
    return postForm<SkillInfo>('/skills', form)
  },

  /** 删除技能目录 */
  remove: (name: string) => del<{ ok: boolean }>(`/skills/${name}`),
}
