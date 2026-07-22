/* Rubric 编辑器（复用于主代理 / 子代理）—— 对应后端 RubricConfig。
   value 为 null 表示该节点不启用 rubric；开关切换启用/关闭。 */
import { Card, Flex, Input, InputNumber, Select, Switch } from 'antd'
import { type ReactNode } from 'react'
import { useMcpTools } from '@/hooks/useMcp'
import type { RubricConfig } from '@/types'

export function RubricEditor({
  value,
  onChange,
  title = '评审 Rubric（节点自评）',
}: {
  value: RubricConfig | null | undefined
  onChange: (v: RubricConfig | null) => void
  title?: string
}): ReactNode {
  const tools = useMcpTools()
  const enabled = !!value
  const cur: RubricConfig = value ?? { content: '', fixed: false, max_iterations: 3, grader_tool_names: [] }

  const patch = (p: Partial<RubricConfig>) => onChange({ ...cur, ...p })

  return (
    <Card size="small" title={title}>
      <Flex align="center" gap={8} style={{ marginBottom: enabled ? 12 : 0 }}>
        <Switch checked={enabled} onChange={(on) => onChange(on ? { ...cur } : null)} />
        <span style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>
          {enabled ? '已启用' : '未启用'}
        </span>
      </Flex>
      {enabled ? (
        <>
          <Input.TextArea
            rows={4}
            value={cur.content ?? ''}
            onChange={(e) => patch({ content: e.target.value })}
            placeholder="评分准则文本（grader 据此评审节点输出）"
            style={{ marginBottom: 8 }}
          />
          <Flex gap={24} wrap>
            <Flex align="center" gap={6}>
              <span style={{ fontSize: 13 }}>常驻：</span>
              <Switch checked={!!cur.fixed} onChange={(v) => patch({ fixed: v })} />
              <span style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>
                (FixedRubric 无显式 rubric 时注入)
              </span>
            </Flex>
            <Flex align="center" gap={6}>
              <span style={{ fontSize: 13 }}>最大评审轮数：</span>
              <InputNumber
                min={1}
                max={20}
                value={cur.max_iterations ?? 3}
                onChange={(v) => patch({ max_iterations: typeof v === 'number' ? v : 3 })}
              />
            </Flex>
          </Flex>
          <div style={{ marginTop: 8 }}>
            <span style={{ fontSize: 13 }}>grader 取证工具：</span>
            <Select
              mode="multiple"
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择 grader 可调用的工具"
              value={cur.grader_tool_names ?? []}
              onChange={(v) => patch({ grader_tool_names: v })}
              options={(tools.data ?? []).map((t) => ({ value: t.name, label: t.name }))}
            />
          </div>
        </>
      ) : null}
    </Card>
  )
}
