/* Rubric 编辑器（复用于主代理 / 子代理）—— 对应后端 RubricConfig。
   嵌入式去壳呈现（原 antd Card 卡中卡改为 rubric-block）：直接平铺在
   详情面板的"行为与评审"分区内，标题 + 启用开关同行。
   value 为 null 表示该节点不启用 rubric；开关切换启用/关闭。
   fixed 两态决定评审依据来源（镜像后端 RubricConfig 校验）：
     - 非常驻（按需）：不出现规则填写框，准则由会话内每次调用传入，不传则不评审；
     - 常驻：出现规则填写框且必填（grader=jev 可只写准则清单，由其序列化生成）。
   grader=jev（JEV 门控分诊）：准则清单（statement + 行级阈值）+ 节点阈值 +
   真值表预览；能力位（jev_rubric_enabled）驱动模式可见性。 */
import { Empty, Flex, Input, InputNumber, Select, Switch, Tag, Tooltip, Typography, Button } from 'antd'
import { type ReactNode } from 'react'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useMcpTools } from '@/hooks/useMcp'
import { DragTextarea } from './DragTextarea'
import type { RubricConfig } from '@/types'

const { Text } = Typography

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
  const caps = useCapabilities()
  const enabled = !!value
  const cur: RubricConfig = value ?? { content: '', fixed: false, max_iterations: 3, grader_tool_names: [] }
  const isJev = cur.grader === 'jev'
  const isFixed = !!cur.fixed
  const criteria = cur.criteria ?? []

  const patch = (p: Partial<RubricConfig>) => onChange({ ...cur, ...p })

  const patchCriterion = (i: number, p: Partial<{ statement: string; threshold: number | null }>) => {
    patch({ criteria: criteria.map((c, idx) => (idx === i ? { ...c, ...p } : c)) })
  }

  // 客户端校验（镜像后端 validator：后端为准，此处为即时反馈）
  const criteriaValid = criteria.length > 0 && criteria.every((c) => (c.statement || '').trim())
  // 常驻模式必须有可注入的固定准则：规则文本，或 jev 的准则清单（序列化生成）
  const hasContent = !!(cur.content ?? '').trim()
  const fixedRulesMissing = isFixed && !hasContent && !criteria.length

  return (
    <div className="rubric-block">
      <Flex align="center" gap={8} wrap style={{ marginBottom: enabled ? 12 : 0 }}>
        <span className="rubric-block__title">{title}</span>
        <Switch checked={enabled} onChange={(on) => onChange(on ? { ...cur } : null)} />
        <span style={{ color: 'var(--color-text-secondary)', fontSize: 12 }}>
          {enabled ? '已启用' : '未启用'}
        </span>
      </Flex>
      {enabled ? (
        <>
          <Flex gap={24} wrap style={{ marginBottom: 8 }}>
            <Flex align="center" gap={6}>
              <span style={{ fontSize: 13 }}>常驻：</span>
              <Switch checked={isFixed} onChange={(v) => patch({ fixed: v })} />
              <span style={{ color: 'var(--color-text-tertiary)', fontSize: 12 }}>
                {isFixed ? '(固定准则每回合注入，需在下方填写规则)' : '(按需：准则由会话内传入，不传不评审)'}
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
          {isFixed ? (
            <>
              <DragTextarea
                rows={4}
                value={cur.content ?? ''}
                onChange={(e) => patch({ content: e.target.value })}
                placeholder={
                  isJev
                    ? '评分准则文本（可留空：未填时以下方准则清单序列化生成，LLM 与 JEV 评同一份）'
                    : '评分准则文本（必填，grader 据此评审节点输出）'
                }
                wrapStyle={{ marginBottom: fixedRulesMissing ? 4 : 8 }}
              />
              {fixedRulesMissing && (
                <div style={{ marginBottom: 8 }}>
                  <Text type="danger" style={{ fontSize: 12 }}>
                    常驻模式必须填写评分准则
                    {isJev ? '，或在下方的准则清单中至少添加一条达标准则' : ''}
                  </Text>
                </div>
              )}
            </>
          ) : (
            <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
              按需模式：无需填写默认规则，在会话输入框展开「本次 rubric 覆盖」传入准则，
              仅传入准则的回合会触发评审。
            </Text>
          )}
          <Flex align="center" gap={8} wrap>
            <span style={{ fontSize: 13 }}>grader 模式：</span>
            <Select
              style={{ width: 220 }}
              value={cur.grader ?? 'llm'}
              onChange={(v) => patch({ grader: v })}
              options={[
                { value: 'llm', label: 'LLM 评审（默认）' },
                {
                  value: 'jev',
                  label: 'JEV 门控（快道分诊）',
                  disabled: !caps.jev_rubric_enabled,
                },
              ]}
            />
            {!caps.jev_rubric_enabled && (
              <Tooltip title="后端未启用 JEV rubric 门控（JEV_RUBRIC_ENABLED/TYPESAFE_API_KEY）">
                <Tag color="default">平台能力未启用</Tag>
              </Tooltip>
            )}
            {isJev && (
              <Flex align="center" gap={6}>
                <Tooltip title="节点级快道阈值（P 与置信度双轴同值）：高=保守多走灰区（LLM 兜底），低=激进多走快道。行级阈值可逐条覆盖；缺省用平台全局值">
                  <span style={{ fontSize: 13 }}>快道阈值：</span>
                </Tooltip>
                <InputNumber
                  min={0.5}
                  max={0.99}
                  step={0.05}
                  value={cur.pass_threshold ?? undefined}
                  placeholder="平台默认"
                  onChange={(v) => patch({ pass_threshold: typeof v === 'number' ? v : null })}
                />
              </Flex>
            )}
          </Flex>
          {isJev && (
            <div style={{ marginTop: 12 }}>
              <Flex align="center" justify="space-between" style={{ marginBottom: 4 }}>
                <span style={{ fontSize: 13 }}>达标准则（须全部满足）：</span>
                <Button
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={() => patch({ criteria: [...criteria, { statement: '' }] })}
                >
                  添加准则
                </Button>
              </Flex>
              {criteria.length === 0 ? (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      写正向达标断言（不写否定句），示例见输入框占位
                    </Text>
                  }
                />
              ) : (
                <Flex vertical gap={8}>
                  {criteria.map((c, i) => (
                    <Flex key={i} gap={8} align="center">
                      <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)', minWidth: 44 }}>
                        准则 {i + 1}
                      </span>
                      <Input
                        value={c.statement}
                        onChange={(e) => patchCriterion(i, { statement: e.target.value })}
                        placeholder="达标断言（仅凭对话记录可核实），如：回答中每个统计数据均标注了来源"
                        style={{ flex: 1 }}
                      />
                      <Tooltip title="行级阈值覆盖（P 与 conf 双轴同值），留空继承节点/平台阈值">
                        <InputNumber
                          min={0.5}
                          max={0.99}
                          step={0.05}
                          size="small"
                          value={c.threshold ?? undefined}
                          placeholder="阈值"
                          onChange={(v) => patchCriterion(i, { threshold: typeof v === 'number' ? v : null })}
                        />
                      </Tooltip>
                      <Button
                        size="small"
                        type="text"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => patch({ criteria: criteria.filter((_, idx) => idx !== i) })}
                      />
                    </Flex>
                  ))}
                </Flex>
              )}
              {!criteriaValid && (
                <div style={{ marginTop: 4 }}>
                  <Text type="danger" style={{ fontSize: 12 }}>
                    jev 模式需要至少一条非空达标准则
                  </Text>
                </div>
              )}
              <div className="rubric-block__rules">
                <Flex vertical gap={2}>
                  <Text style={{ fontSize: 12 }}>门控规则（首评冻结准则后生效）</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · 快道放行：全部准则 P≥t 且 conf≥t，跳过 LLM 评审
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · 阈值 t：行级 → 节点
                    {cur.pass_threshold != null ? `（现值 ${cur.pass_threshold}）` : ''} → 平台默认
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · 其余一律走 LLM 评审兜底（含 JEV 不可用）
                  </Text>
                </Flex>
              </div>
            </div>
          )}
          <div style={{ marginTop: 8 }}>
            <span style={{ fontSize: 13 }}>grader 取证工具：</span>
            <Select
              mode="multiple"
              style={{ width: '100%', marginTop: 4 }}
              placeholder="选择 grader 可调用的工具（非空时 JEV 门控不参与——证据型评审不可概率短路）"
              value={cur.grader_tool_names ?? []}
              onChange={(v) => patch({ grader_tool_names: v })}
              options={(tools.data ?? []).map((t) => ({ value: t.name, label: t.name }))}
            />
          </div>
        </>
      ) : null}
    </div>
  )
}
