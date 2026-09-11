/* 技能描述文本 —— 懒取 GET /skills/{name}/description（SKILL.md frontmatter，
   缺失回退 README.md）。SkillsPage 行展开查看描述用；react-query 按 name
   缓存（与技能列表共享 ["skills"] 失效前缀）。
   full=false 单行省略 + tooltip；full=true 完整展示（展开区用）。 */
import { Typography } from 'antd'
import { type ReactNode } from 'react'
import { useSkillDescription } from '@/hooks/useSkills'

export function SkillDescriptionText({
  name,
  full = false,
}: {
  name: string
  full?: boolean
}): ReactNode {
  const q = useSkillDescription(name)
  if (q.isLoading) {
    return <Typography.Text type="secondary">描述加载中…</Typography.Text>
  }
  if (q.isError) {
    return <Typography.Text type="secondary">描述获取失败</Typography.Text>
  }
  const desc = q.data?.description?.trim()
  if (!desc) {
    return <Typography.Text type="secondary">暂无描述（技能内未找到含 frontmatter 的 SKILL.md / README.md）</Typography.Text>
  }
  if (full) {
    return (
      <Typography.Text type="secondary" style={{ whiteSpace: 'pre-wrap' }}>
        {desc}
      </Typography.Text>
    )
  }
  return (
    <Typography.Text type="secondary" ellipsis={{ tooltip: desc }}>
      {desc}
    </Typography.Text>
  )
}
