/* AgentConfig 节点编辑器（递归）—— 主代理 path=[]；子代理 path=[i,...]。
   由 agentEditorStore (immer) 驱动就地修改。前端预校验体验，最终以后端 /validate 为准（准则 F6）。
   主代理独有 memory/workspace；子代理独有 name/description/is_dicted。 */
import { Button, Card, Collapse, Flex, Input, Select, Switch, Tag, Typography } from 'antd'
import { type ReactNode } from 'react'
import { ChevronRight, Plus, Trash2 } from 'lucide-react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { useMcpTools } from '@/hooks/useMcp'
import { useSkills } from '@/hooks/useSkills'
import { RubricEditor } from './RubricEditor'
import { getNode, newSubagent, type NodePath } from './nodeHelpers'
import type { AgentConfig } from '@/types'

export function NodeEditor({ path, defaultOpen }: { path: NodePath; defaultOpen?: boolean }): ReactNode {
  const config = useAgentEditorStore((s) => s.config)
  const mutate = useAgentEditorStore((s) => s.mutate)
  const tools = useMcpTools()
  const skills = useSkills()

  if (!config) return null
  const node = getNode(config, path)
  const isMain = path.length === 0

  const onMutate = (fn: (n: AgentConfig) => void) => mutate((draft) => fn(getNode(draft, path)))

  const toolOptions = (tools.data ?? []).map((t) => ({ value: t.name, label: t.name }))
  const skillOptions = (skills.data ?? []).map((s) => ({ value: `/skills/${s.name}/`, label: s.name }))
  const interruptKeys = Object.entries(node.interrupt_on ?? {})
    .filter(([, v]) => v)
    .map(([k]) => k)

  const addSubagent = () =>
    onMutate((n) => {
      if (!n.subagents) n.subagents = []
      n.subagents.push(newSubagent())
    })

  const removeSubagent = (i: number) =>
    mutate((draft) => {
      getNode(draft, path).subagents?.splice(i, 1)
    })

  const header = (
    <Flex align="center" gap={8}>
      <ChevronRight size={14} />
      <span>{isMain ? '主代理' : `子代理：${node.name || '未命名'}`}</span>
      {node.is_dicted ? <Tag color="purple">dicted</Tag> : null}
    </Flex>
  )

  return (
    <Collapse
      defaultActiveKey={defaultOpen ? ['1'] : undefined}
      items={[
        {
          key: '1',
          label: header,
          children: (
            <Flex vertical gap={12}>
              {/* 子代理独有：name / description */}
              {!isMain ? (
                <>
                  <Field label="子代理名称（必填）">
                    <Input
                      value={node.name ?? ''}
                      onChange={(e) => onMutate((n) => (n.name = e.target.value))}
                      placeholder="如 researcher"
                    />
                  </Field>
                  <Field label="子代理描述（必填）">
                    <Input
                      value={node.description ?? ''}
                      onChange={(e) => onMutate((n) => (n.description = e.target.value))}
                      placeholder="一句话职责"
                    />
                  </Field>
                </>
              ) : null}

              <Field label="system_prompt（必填）">
                <Input.TextArea
                  rows={6}
                  value={node.system_prompt}
                  onChange={(e) => onMutate((n) => (n.system_prompt = e.target.value))}
                  placeholder="代理的系统提示词"
                />
              </Field>

              <Field label="工具（tool_names，多选 MCP 工具）">
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder="选择命名空间工具（{server}__{tool}）"
                  value={node.tool_names ?? []}
                  onChange={(v) => onMutate((n) => (n.tool_names = v))}
                  options={toolOptions}
                />
              </Field>

              <Field label="技能（skills）">
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder="引用 /skills/<name>/"
                  value={node.skills ?? []}
                  onChange={(v) => onMutate((n) => (n.skills = v.length ? v : null))}
                  options={skillOptions}
                />
              </Field>

              <Field label="工具调用审批（interrupt_on）">
                <Select
                  mode="multiple"
                  style={{ width: '100%' }}
                  placeholder="选择需要人工审批的工具（声明式 HITL）"
                  value={interruptKeys}
                  onChange={(v) =>
                    onMutate((n) => {
                      n.interrupt_on = v.length
                        ? Object.fromEntries(v.map((k) => [k, true]))
                        : null
                    })
                  }
                  options={toolOptions}
                />
              </Field>

              {/* 主代理独有：memory / workspace */}
              {isMain ? (
                <>
                  <Field label="偏好记忆（memory，仅主代理）">
                    <Select
                      mode="tags"
                      style={{ width: '100%' }}
                      placeholder="形如 /memories/preferences.md"
                      value={node.memory ?? []}
                      onChange={(v) => onMutate((n) => (n.memory = v.length ? v : null))}
                    />
                  </Field>
                  <Field label="工作区目录（workspace，仅主代理）">
                    <Input
                      value={node.workspace ?? ''}
                      onChange={(e) => onMutate((n) => (n.workspace = e.target.value || null))}
                      placeholder="相对 LOCAL_ROOT 的单一目录名（如 workspace）"
                    />
                  </Field>
                </>
              ) : (
                <Field label="dicted 模式（仅子代理）">
                  <Switch
                    checked={!!node.is_dicted}
                    onChange={(v) => onMutate((n) => (n.is_dicted = v))}
                  />
                  <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                    dicted 子代理不可再嵌套 subagents
                  </Typography.Text>
                </Field>
              )}

              <Field label="附件能力（allow_attachments）">
                <Switch
                  checked={!!node.allow_attachments}
                  onChange={(v) => onMutate((n) => (n.allow_attachments = v))}
                />
              </Field>

              <RubricEditor
                value={node.rubric}
                onChange={(v) => onMutate((n) => (n.rubric = v))}
              />

              {/* 递归子代理（dicted 节点禁止嵌套） */}
              {isMain || !node.is_dicted ? (
                <Card size="small" title={`子代理（委派）${node.subagents?.length ? `· ${node.subagents.length}` : ''}`}>
                  <Flex vertical gap={8}>
                    {node.subagents?.map((_, i) => (
                      <Flex key={i} gap={8} align="flex-start">
                        <div style={{ flex: 1 }}>
                          <NodeEditor path={[...path, i]} />
                        </div>
                        <Button
                          type="link"
                          danger
                          size="small"
                          icon={<Trash2 size={14} />}
                          onClick={() => removeSubagent(i)}
                        />
                      </Flex>
                    ))}
                    <Button block icon={<Plus size={14} />} onClick={addSubagent}>
                      添加子代理
                    </Button>
                  </Flex>
                </Card>
              ) : null}
            </Flex>
          ),
        },
      ]}
    />
  )
}

function Field({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div>
      <div style={{ marginBottom: 4, fontSize: 13, color: 'var(--color-text-secondary)' }}>{label}</div>
      {children}
    </div>
  )
}
