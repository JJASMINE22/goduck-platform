/* 技能 API —— 对应后端 api/skills.py（prefix /skills）。
   全局 skills 池共享，一个 skill = 一个子目录。multipart 上传防目录穿越。 */
import { del, get, postForm } from './client'
import type { SkillInfo } from '@/types'

export const skillsApi = {
  /** 列出技能（子目录 + 文件数） */
  list: () => get<SkillInfo[]>('/skills'),

  /** multipart 上传：name + 多文件 → 写入 {pool}/<name>/ */
  upload: (name: string, files: File[]) => {
    const form = new FormData()
    form.append('name', name)
    for (const f of files) form.append('files', f, f.name)
    return postForm<SkillInfo>('/skills', form)
  },

  /** 删除技能目录 */
  remove: (name: string) => del<{ ok: boolean }>(`/skills/${name}`),
}
