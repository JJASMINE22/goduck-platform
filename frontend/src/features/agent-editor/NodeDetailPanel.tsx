/* 节点详情面板（右侧）—— 编辑画布上选中的节点字段。
   展示层（方案 A+C）：语义四分区玻璃卡（身份 / 能力 / 环境 / 行为与评审），
   区头 sticky + 实时摘要徽标 + 可折叠；短字段（开关）双列网格单元，长字段
   （system_prompt / 工具树 / rubric）整行。分区色：身份随节点角色色，能力主色，
   环境青色，行为与评审琥珀。
   逻辑不变：按 selectedNodeId 从 store 取节点，updateFields(id, patch) 就地合并；
   主代理独有 memory/allow_attachments；子代理独有 name/description/is_dicted；
   workspace 主子代理均可。dicted 叶子的后端三键（workspace/allow_shell/shell_guard）
   随主代理生效（deepagents：子代理经 FilesystemMiddleware(backend=父backend) 共享
   父后端，节点配置不参与构建）——dicted 节点置灰三开关、回显恒为关（后端校验层
   剥离为 False 落库），勾选 dicted 时编辑器预清除。cron 用途不支持的能力（工具审批
   interrupt_on / 附件 allow_attachments / 知识库 allow_knowledge / Shell 终端执行
   allow_shell）整块不渲染：不显示占位说明，仅留【cron 不支持·已隐藏】注释（后端
   validator 仍为最终权威，准则 F6）。 */
import { Button, Flex, Input, Select, Tag, TreeSelect, Typography } from 'antd'
import { type ReactNode } from 'react'
import { Fingerprint, FlaskConical, HardDrive, MousePointerClick, Trash2, Wrench } from 'lucide-react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useMcpTools } from '@/hooks/useMcp'
import { useSkills } from '@/hooks/useSkills'
import { RubricEditor } from './RubricEditor'
import { CheckedToolGroups } from './CheckedToolGroups'
import { DragTextarea } from './DragTextarea'
import { DetailSection, SwitchField } from './DetailSection'
import type { NodeFields } from './graphModel'

/** 摘要徽标用：开/关 短词 */
const sw = (b?: boolean) => (b ? '开' : '关')

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
  // dicted 叶子：后端三键（workspace/allow_shell/shell_guard）随主代理生效，
  // 节点配置不参与构建——编辑器置灰 + 回显恒为关（与后端校验层剥离语义一致）
  const isDicted = !isMain && !!node.is_dicted
  const effWorkspace = isDicted ? false : !!node.workspace
  const effAllowShell = isDicted ? false : !!node.allow_shell
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

  // —— 分区摘要（区头徽标，随配置实时变化；只统计当前实际渲染的项） ——
  const promptLen = (node.system_prompt ?? '').length
  const capabilitySummary =
    `工具 ${selectedTools.length} · 技能 ${(node.skills ?? []).length}` +
    (kind !== 'cron' ? ` · 审批 ${interruptKeys.length}` : '')
  // 环境分区启用计数：与下方渲染条件严格同源的布尔集（null = 该项不渲染）；
  // dicted 节点的 workspace/allow_shell 以置灰回显值（恒 false）计入
  const envFlags = [
    isMain ? !!node.memory : null,
    isMain && kind !== 'cron' ? !!node.allow_attachments : null,
    isMain && kind !== 'cron' ? !!node.allow_knowledge : null,
    effWorkspace,
    kind !== 'cron' ? effAllowShell : null,
  ].filter((v): v is boolean => v !== null)
  const behaviorSummary = [
    !node.is_dicted ? `分发 ${sw(node.dynamic_dispatch)}` : null,
    !isMain ? `dicted ${sw(node.is_dicted)}` : null,
    `规划 ${sw(node.task_planning)}`,
    `评审 ${sw(!!node.rubric)}`,
  ]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="node-detail">
      <Flex align="center" justify="space-between" style={{ marginBottom: 10 }}>
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

      <div className="node-detail__sections">
        {/* —— 身份：命名与提示词（子代理含名称/描述） —— */}
        <DetailSection
          icon={<Fingerprint size={13} />}
          title="身份"
          tone={color}
          summary={`提示词 ${promptLen} 字`}
        >
          {!isMain ? (
            <Field label="子代理名称（必填）">
              <Input
                value={node.name ?? ''}
                onChange={(e) => patch({ name: e.target.value })}
                placeholder="如 researcher"
              />
            </Field>
          ) : null}
          {!isMain ? (
            <Field label="子代理描述（必填）" wide>
              <DragTextarea
                rows={1}
                value={node.description ?? ''}
                onChange={(e) => patch({ description: e.target.value })}
                placeholder="一句话职责（较长可拖底部抓手调大查看）"
              />
            </Field>
          ) : null}
          <Field label="system_prompt（必填）" wide>
            <DragTextarea
              rows={6}
              value={node.system_prompt}
              onChange={(e) => patch({ system_prompt: e.target.value })}
              placeholder="代理的系统提示词（较长可拖底部抓手调大通览）"
            />
          </Field>
        </DetailSection>

        {/* —— 能力：工具 / 技能 / 审批（长控件整行） —— */}
        <DetailSection
          icon={<Wrench size={13} />}
          title="能力"
          tone="var(--color-primary)"
          summary={capabilitySummary}
        >
          <Field label="工具（tool_names，按 server 分组）" wide>
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
          <Field label="技能（skills）" wide>
            <Select
              mode="multiple"
              style={{ width: '100%' }}
              placeholder="选择技能"
              value={node.skills ?? []}
              onChange={(v) => patch({ skills: v.length ? v : null })}
              options={skillOptions}
            />
          </Field>
          {/* 【cron 不支持·已隐藏】工具调用审批：定时任务无人值守、无审批通道 */}
          {kind !== 'cron' ? (
            <Field label="工具调用审批（interrupt_on，仅已选工具）" wide>
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
        </DetailSection>

        {/* —— 环境：挂载与能力开关（双列网格单元） —— */}
        <DetailSection
          icon={<HardDrive size={13} />}
          title="环境"
          tone="var(--color-role-agent-sub)"
          summary={`已启用 ${envFlags.filter(Boolean).length}/${envFlags.length}`}
        >
          {isMain ? (
            <SwitchField
              label="偏好记忆（memory）"
              desc="开启后挂载 /memories/（偏好文件由后端固定：preferences.md / style.md）"
              checked={!!node.memory}
              onChange={(v) => patch({ memory: v })}
            />
          ) : null}
          {/* 【cron 不支持·已隐藏】附件能力：定时任务输入为固定 prompt，无消息装配通道 */}
          {isMain && kind !== 'cron' ? (
            <SwitchField
              label="附件能力（allow_attachments）"
              desc="开启后才可下挂视觉模型（基座无视觉时图像经其描述预消费）"
              checked={!!node.allow_attachments}
              onChange={(v) => patch({ allow_attachments: v })}
            />
          ) : null}
          {/* 【cron 不支持·已隐藏】知识库能力：同上 */}
          {isMain && kind !== 'cron' ? (
            <SwitchField
              label="知识库能力（allow_knowledge）"
              checked={!!node.allow_knowledge}
              onChange={(v) => patch({ allow_knowledge: v })}
            />
          ) : null}
          <SwitchField
            label="工作区（workspace）"
            desc={
              isDicted
                ? 'dicted 叶子共享主代理后端，此开关不生效（随主代理）'
                : '开启后挂载 /workspace/（路径后端固定为 LOCAL_ROOT/workspace，写入即落盘、跨代理可见）'
            }
            checked={effWorkspace}
            disabled={isDicted}
            onChange={(v) => patch({ workspace: v })}
          />
          {/* 【cron 不支持·已隐藏】Shell 终端执行：定时任务无人值守、无审批通道 */}
          {kind !== 'cron' ? (
            <>
              <SwitchField
                label="Shell 执行（allow_shell）"
                desc={
                  isDicted
                    ? 'dicted 叶子共享主代理后端，此开关不生效（execute 随主代理）'
                    : '开启后获得 execute 工具（per-user 工作目录，每条命令强制人工审批）。主代理与 compiled 子代理均可设。'
                }
                checked={effAllowShell}
                disabled={isDicted}
                onChange={(v) => patch({ allow_shell: v, shell_guard: v ? node.shell_guard : false })}
              />
              {effAllowShell ? (
                <div className="detail-subnote">
                  <Typography.Text type="warning" style={{ fontSize: 12 }}>
                    ⚠ 本地 shell 非安全边界：命令可访问宿主机任意路径（含其他用户文件与系统密钥），仅适用于单信任域部署。
                  </Typography.Text>
                  {/* shell_guard：allow_shell 开启 ∧ 后端 jev 能力可用时可见（DESIGN F2 能力驱动） */}
                  {capabilities.jev_enabled ? (
                    <SwitchField
                      label="Jev 守卫（shell_guard）"
                      desc="只读命令直通；灰区命令按校准概率门控（shadow 仅标注 / enforce 高置信自动放行）；包安装与判定失败一律问人。"
                      checked={!!node.shell_guard}
                      onChange={(v) => patch({ shell_guard: v })}
                    />
                  ) : null}
                </div>
              ) : null}
            </>
          ) : null}
        </DetailSection>

        {/* —— 行为与评审：编排行为开关 + rubric（去壳嵌入） —— */}
        <DetailSection
          icon={<FlaskConical size={13} />}
          title="行为与评审"
          tone="var(--color-warning)"
          summary={behaviorSummary}
        >
          {!node.is_dicted ? (
            <SwitchField
              label="动态分发（dynamic_dispatch）"
              desc={
                hasChildren
                  ? '启用后用 JS 解释器 task() 动态编排下挂子代理（须为 dicted 叶子）'
                  : '需先下挂子代理才能启用'
              }
              checked={!!node.dynamic_dispatch}
              disabled={!hasChildren}
              onChange={(v) => patch({ dynamic_dispatch: v })}
            />
          ) : null}
          {!isMain ? (
            <SwitchField
              label="dicted 模式（仅子代理）"
              desc="dicted 子代理不可再嵌套 subagents；工作区/Shell 后端随主代理生效（本节点开关不参与构建）"
              checked={!!node.is_dicted}
              onChange={(v) =>
                patch(
                  v
                    ? { is_dicted: true, workspace: false, allow_shell: false, shell_guard: false }
                    : { is_dicted: false },
                )
              }
            />
          ) : null}
          <SwitchField
            label="任务规划（task_planning）"
            desc="开启后该代理获得 write_todos 工具，执行中实时推送任务清单（会话窗顶部进度面板）。per-node 不继承，主代理与子代理均可独立开启。"
            checked={!!node.task_planning}
            onChange={(v) => patch({ task_planning: v })}
          />
          <div className="detail-field detail-field--wide">
            <RubricEditor value={node.rubric} onChange={(v) => patch({ rubric: v })} />
          </div>
        </DetailSection>
      </div>
    </div>
  )
}

/** 字段单元：标签 + 控件；wide = 整行（跨双列网格） */
function Field({ label, wide, children }: { label: string; wide?: boolean; children: ReactNode }): ReactNode {
  return (
    <div className={wide ? 'detail-field detail-field--wide' : 'detail-field'}>
      <div className="detail-field__label">{label}</div>
      {children}
    </div>
  )
}
