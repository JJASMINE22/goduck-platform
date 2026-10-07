/* 节点详情面板（右侧）—— 编辑画布上选中的节点字段。
   按 selectedNodeId 从 store 取节点，updateFields(id, patch) 就地合并。
   主代理独有 memory/allow_attachments；子代理独有 name/description/is_dicted；workspace 主子代理均可。
   cron 用途智能体不支持的能力（工具审批 interrupt_on / 附件 allow_attachments /
   知识库 allow_knowledge / Shell 终端执行 allow_shell）整块不渲染：
   不显示占位说明，仅在源码留【cron 不支持·已隐藏】注释，避免为创建者增添理解成本
   （后端 validator 仍为最终权威）。
   最终合法性以后端 /validate 为准（准则 F6）。 */
import { Button, Flex, Input, Select, Switch, Tag, TreeSelect, Typography } from 'antd'
import { type ReactNode } from 'react'
import { MousePointerClick, ShieldCheck, Trash2 } from 'lucide-react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useMcpTools } from '@/hooks/useMcp'
import { useSkills } from '@/hooks/useSkills'
import { RubricEditor } from './RubricEditor'
import { CheckedToolGroups } from './CheckedToolGroups'
import type { NodeFields } from './graphModel'

export function NodeDetailPanel(): ReactNode {
  const nodes = useAgentEditorStore((s) => s.nodes)
  const selectedId = useAgentEditorStore((s) => s.selectedNodeId)
  const updateFields = useAgentEditorStore((s) => s.updateFields)
  const removeNode = useAgentEditorStore((s) => s.removeNode)
  const kind = useAgentEditorStore((s) => s.kind)
  const tools = useMcpTools()
  const skills = useSkills()
  const capabilities = useCapabilities()

  const node = nodes?.find((n) => n.id === selectedId) ?? null

  if (!node) {
    return (
      <div className="node-detail node-detail--empty">
        <div className="node-detail__hint">
          <MousePointerClick size={30} />
          <p>点击画布上的节点</p>
          <small>在此编辑系统提示词、工具、技能等</small>
        </div>
      </div>
    )
  }

  const isMain = !!node.is_main
  const hasChildren = nodes?.some((n) => n.parentId === node.id) ?? false
  const color = isMain ? 'var(--color-role-agent-main)' : 'var(--color-role-agent-sub)'
  // 按 server 分组的工具树：{server}__{tool} → server 节点(可全选) + tool 叶子
  const toolTreeData = (() => {
    const byServer = new Map<string, string[]>()
    for (const t of tools.data ?? []) {
      const idx = t.name.indexOf('__')
      const server = idx > 0 ? t.name.slice(0, idx) : '其他'
      const arr = byServer.get(server) ?? []
      arr.push(t.name)
      byServer.set(server, arr)
    }
    return Array.from(byServer.entries()).map(([server, names]) => ({
      title: `${server}（${names.length}）`,
      value: `__group__${server}`,
      selectable: false,
      children: names.map((fullName) => {
        const idx = fullName.indexOf('__')
        return { title: idx > 0 ? fullName.slice(idx + 2) : fullName, value: fullName }
      }),
    }))
  })()
  const skillOptions = (skills.data ?? []).map((s) => ({ value: `/skills/${s.name}/`, label: s.name }))
  // 审批选项收敛到本代理已选工具（只能对挂载的工具设审批），按 server 分组折叠
  const selectedTools = node.tool_names ?? []
  const interruptTreeData = toolTreeData
    .map((g) => ({ ...g, children: (g.children ?? []).filter((c) => selectedTools.includes(c.value)) }))
    .filter((g) => (g.children ?? []).length > 0)
  const interruptKeys = Object.entries(node.interrupt_on ?? {})
    .filter(([, v]) => v)
    .map(([k]) => k)
    .filter((k) => selectedTools.includes(k))

  const patch = (p: Partial<NodeFields>) => updateFields(node.id, p)

  // 移除某工具：同步清理其 interrupt_on 审批项，避免残留无效审批配置
  const removeTool = (fullName: string) => {
    const nextTools = (node.tool_names ?? []).filter((n) => n !== fullName)
    const p: Partial<NodeFields> = { tool_names: nextTools }
    if (node.interrupt_on?.[fullName]) {
      const rest = { ...node.interrupt_on }
      delete rest[fullName]
      p.interrupt_on = Object.keys(rest).length ? rest : null
    }
    patch(p)
  }

  return (
    <div className="node-detail">
      <Flex align="center" justify="space-between" style={{ marginBottom: 12 }}>
        <Flex align="center" gap={8}>
          <span className="node-detail__dot" style={{ background: color }} />
          <Typography.Text strong>{isMain ? '主代理' : '子代理'}</Typography.Text>
          {node.is_dicted ? <Tag color="purple">dicted</Tag> : null}
          {node.dynamic_dispatch ? <Tag color="gold">dynamic</Tag> : null}
        </Flex>
        {!isMain ? (
          <Button size="small" danger icon={<Trash2 size={13} />} onClick={() => removeNode(node.id)}>
            删除
          </Button>
        ) : null}
      </Flex>

      <Flex vertical gap={12}>
        {!isMain ? (
          <>
            <Field label="子代理名称（必填）">
              <Input value={node.name ?? ''} onChange={(e) => patch({ name: e.target.value })} placeholder="如 researcher" />
            </Field>
            <Field label="子代理描述（必填）">
              <Input value={node.description ?? ''} onChange={(e) => patch({ description: e.target.value })} placeholder="一句话职责" />
            </Field>
          </>
        ) : null}

        <Field label="system_prompt（必填）">
          <Input.TextArea rows={6} value={node.system_prompt} onChange={(e) => patch({ system_prompt: e.target.value })} placeholder="代理的系统提示词" />
        </Field>

        <Field label="工具（tool_names，按 server 分组）">
          <TreeSelect
            treeCheckable
            showCheckedStrategy={TreeSelect.SHOW_CHILD}
            maxTagCount={2}
            style={{ width: '100%' }}
            placeholder="按 server 分组选择工具"
            value={node.tool_names ?? []}
            onChange={(v) => patch({ tool_names: (v as string[]) ?? [] })}
            treeData={toolTreeData}
            showSearch
            treeNodeFilterProp="label"
            allowClear
          />
          <CheckedToolGroups
            selected={node.tool_names ?? []}
            tools={tools.data ?? []}
            onRemove={removeTool}
          />
        </Field>

        <Field label="技能（skills）">
          <Select
            mode="multiple"
            style={{ width: '100%' }}
            placeholder="选择技能"
            value={node.skills ?? []}
            onChange={(v) => patch({ skills: v.length ? v : null })}
            options={skillOptions}
          />
        </Field>

        {/* 【cron 不支持·已隐藏】工具调用审批（interrupt_on）：定时任务无人值守、
            无审批通道，工具一律自动执行（后端 validator 拒绝 usage=cron ∧
            interrupt_on，为最终权威）。不渲染占位说明，避免为创建者增添理解成本 */}
        {kind !== 'cron' ? (
          <Field label="工具调用审批（interrupt_on，仅已选工具）">
            <TreeSelect
              treeCheckable
              showCheckedStrategy={TreeSelect.SHOW_CHILD}
              maxTagCount={2}
              style={{ width: '100%' }}
              placeholder={interruptTreeData.length ? '勾选需要人工审批的工具（按 server 折叠）' : '请先在上方选择工具'}
              value={interruptKeys}
              onChange={(v) => {
                const arr = (v as string[]) ?? []
                patch({ interrupt_on: arr.length ? Object.fromEntries(arr.map((k) => [k, true])) : null })
              }}
              treeData={interruptTreeData}
              showSearch
              treeNodeFilterProp="label"
              allowClear
              disabled={!interruptTreeData.length}
            />
          </Field>
        ) : null}

        {isMain ? (
          <>
            <Field label="偏好记忆（memory，仅主代理）">
              <Flex align="center" gap={8}>
                <Switch checked={!!node.memory} onChange={(v) => patch({ memory: v })} />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  开启后挂载 /memories/（偏好文件由后端固定：preferences.md / style.md）
                </Typography.Text>
              </Flex>
            </Field>
            {/* 【cron 不支持·已隐藏】附件能力（allow_attachments）/ 知识库能力
                （allow_knowledge）：定时任务输入为固定 prompt，无消息装配通道
                （后端 validator 拒绝 usage=cron ∧ 两开关，为最终权威）。
                不渲染占位说明，避免为创建者增添理解成本 */}
            {kind !== 'cron' ? (
              <>
                <Field label="附件能力（allow_attachments，仅主代理）">
                  <Flex align="center" gap={8}>
                    <Switch checked={!!node.allow_attachments} onChange={(v) => patch({ allow_attachments: v })} />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      开启后才可下挂视觉模型（基座无视觉时图像经其描述预消费）
                    </Typography.Text>
                  </Flex>
                </Field>
                <Field label="知识库能力（allow_knowledge，仅主代理）">
                  <Switch checked={!!node.allow_knowledge} onChange={(v) => patch({ allow_knowledge: v })} />
                </Field>
              </>
            ) : null}
          </>
        ) : (
          <Field label="dicted 模式（仅子代理）">
            <Flex align="center" gap={8}>
              <Switch checked={!!node.is_dicted} onChange={(v) => patch({ is_dicted: v })} />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                dicted 子代理不可再嵌套 subagents
              </Typography.Text>
            </Flex>
          </Field>
        )}

        <Field label="工作区（workspace）">
          <Flex align="center" gap={8}>
            <Switch checked={!!node.workspace} onChange={(v) => patch({ workspace: v })} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              开启后挂载 /workspace/（路径后端固定为 LOCAL_ROOT/workspace，写入即落盘、跨代理可见）
            </Typography.Text>
          </Flex>
        </Field>

        {/* 【cron 不支持·已隐藏】Shell 终端执行（allow_shell）：定时任务无人值守、
            无审批通道，整树禁用（后端 validator 拒绝 usage=cron ∧ allow_shell，
            为最终权威）。不渲染占位说明，避免为创建者增添理解成本 */}
        {kind !== 'cron' ? (
          <Field label="Shell 执行（allow_shell）">
            <Flex vertical gap={4}>
              <Flex align="center" gap={8}>
                <Switch
                  checked={!!node.allow_shell}
                  onChange={(v) => patch({ allow_shell: v, shell_guard: v ? node.shell_guard : false })}
                />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  开启后获得 execute 工具（per-user 工作目录，每条命令强制人工审批）。主代理与子代理均可设。
                </Typography.Text>
              </Flex>
              {node.allow_shell ? (
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  ⚠ 本地 shell 非安全边界：命令可访问宿主机任意路径（含其他用户文件与系统密钥），仅适用于单信任域部署。
                </Typography.Text>
              ) : null}
              {/* shell_guard：allow_shell 开启 ∧ 后端 jev 能力可用时可见（DESIGN F2 能力驱动） */}
              {node.allow_shell && capabilities.jev_enabled ? (
                <Flex align="flex-start" gap={8} style={{ marginTop: 4 }}>
                  <Switch
                    size="small"
                    checked={!!node.shell_guard}
                    onChange={(v) => patch({ shell_guard: v })}
                  />
                  <Flex align="center" gap={4}>
                    <ShieldCheck size={13} />
                    <Typography.Text style={{ fontSize: 12 }}>
                      Jev 守卫：只读命令直通；灰区命令按校准概率门控（shadow 仅标注 / enforce
                      高置信自动放行）；包安装与判定失败一律问人。
                    </Typography.Text>
                  </Flex>
                </Flex>
              ) : null}
            </Flex>
          </Field>
        ) : null}

        {!node.is_dicted ? (
          <Field label="动态分发（dynamic_dispatch）">
            <Flex align="center" gap={8}>
              <Switch
                checked={!!node.dynamic_dispatch}
                disabled={!hasChildren}
                onChange={(v) => patch({ dynamic_dispatch: v })}
              />
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {hasChildren
                  ? '启用后用 JS 解释器 task() 动态编排下挂子代理（须为 dicted 叶子）'
                  : '需先下挂子代理才能启用'}
              </Typography.Text>
            </Flex>
          </Field>
        ) : null}

        <Field label="任务规划（task_planning）">
          <Flex align="center" gap={8}>
            <Switch checked={!!node.task_planning} onChange={(v) => patch({ task_planning: v })} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              开启后该代理获得 write_todos 工具，执行中实时推送任务清单（会话窗顶部进度面板）。
              per-node 不继承，主代理与子代理均可独立开启。
            </Typography.Text>
          </Flex>
        </Field>

        <RubricEditor value={node.rubric} onChange={(v) => patch({ rubric: v })} />
      </Flex>
    </div>
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
