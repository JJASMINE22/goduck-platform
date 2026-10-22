/* 定时任务管理页（DESIGN 模块化布局）—— 对称 /agents /sessions 的顶级页面。
   列表（状态徽标 + 下次运行时间）+ 创建/编辑表单（agent 下拉只列 kind='cron'、
   开始时间「立即/自定义」）+ 启停 + 立即运行 + 运行历史 Drawer + 查看任务会话。
   状态由 useCronJobs 轮询（30s，对齐后端调度 tick 粒度）。 */
import { App as AntApp, Button, DatePicker, Drawer, Flex, Form, Input, Modal, Popconfirm, Select, Switch, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Dayjs } from 'dayjs'
import { CalendarClock, History, MessageSquare, Pencil, Play, Plus, Trash2 } from 'lucide-react'
import { useAgents } from '@/hooks/useAgents'
import { useCronJobs, useCronRuns, useCreateCronJob, useDeleteCronJob, useRunCronJobNow, useUpdateCronJob } from '@/hooks/useCron'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatDateTime, formatRelative } from '@/lib/format'
import { listPager } from '@/lib/pagerConfig'
import type { CronJobOut, CronJobStatus, CronJobUpdate, CronRunOut } from '@/types'

/** last_status → 徽标（运行/成功/失败/待审批；enabled=false 时由调用方覆盖为已停用） */
const STATUS_TAG: Record<CronJobStatus, { color: string; text: string }> = {
  running: { color: 'processing', text: '运行中' },
  ok: { color: 'green', text: '正常' },
  error: { color: 'red', text: '失败' },
  awaiting_approval: { color: 'orange', text: '待审批' },
}

const RUN_STATUS_TAG: Record<string, { color: string; text: string }> = {
  running: { color: 'processing', text: '运行中' },
  ok: { color: 'green', text: '成功' },
  error: { color: 'red', text: '失败' },
  awaiting_approval: { color: 'orange', text: '待审批' },
}

/** 间隔人性化：分钟 → 「N 小时 / N 天 / N 分钟」 */
function intervalLabel(minutes: number): string {
  if (minutes % 1440 === 0 && minutes >= 1440) {
    const d = minutes / 1440
    return d === 7 ? '每周' : `每 ${d} 天`
  }
  if (minutes % 60 === 0 && minutes >= 60) return `每 ${minutes / 60} 小时`
  return `每 ${minutes} 分钟`
}

/** 创建/编辑表单值（start_mode 区分「立即/自定义」；自定义存 Dayjs 便于控件交互） */
interface JobFormValues {
  name: string
  agent_id: string
  prompt: string
  interval_minutes: number
  start_mode: 'now' | 'custom'
  start_at?: Dayjs | null
}

export function CronJobsPage(): ReactNode {
  const navigate = useNavigate()
  const userId = useCurrentUser((s) => s.userId)
  const jobs = useCronJobs(userId)
  const agents = useAgents()
  const createJob = useCreateCronJob()
  const updateJob = useUpdateCronJob()
  const deleteJob = useDeleteCronJob()
  const runNow = useRunCronJobNow()
  const { modal } = AntApp.useApp()

  const [editing, setEditing] = useState<CronJobOut | null>(null) // null=创建
  const [open, setOpen] = useState(false)
  const [runsFor, setRunsFor] = useState<CronJobOut | null>(null)
  const [form] = Form.useForm<JobFormValues>()

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ start_mode: 'now' })
    setOpen(true)
  }

  const openEdit = (job: CronJobOut) => {
    setEditing(job)
    form.resetFields()
    form.setFieldsValue({
      name: job.name,
      agent_id: job.agent_id,
      prompt: job.prompt,
      interval_minutes: job.interval_minutes,
      // 编辑态默认「立即」：不显式重锚（start_at 未提供则调度不动），语义最不意外
      start_mode: 'now',
    })
    setOpen(true)
  }

  /** 常用间隔选项（分钟）—— 输入框可自由覆盖 */
  const INTERVAL_OPTIONS = [
    { value: 30, label: '每 30 分钟' },
    { value: 60, label: '每 1 小时' },
    { value: 360, label: '每 6 小时' },
    { value: 720, label: '每 12 小时' },
    { value: 1440, label: '每天' },
    { value: 10080, label: '每周' },
  ]

  const handleSubmit = async () => {
    const v = await form.validateFields()
    // 自定义开始时间 → ISO 8601（dayjs.toISOString 带时区偏移，后端 timestamptz 零歧义）
    const startAt = v.start_mode === 'custom' && v.start_at ? v.start_at.toISOString() : null
    try {
      if (editing) {
        // 只提交变更字段：后端「schedule 字段出现即重锚」——仅改 prompt/name
        // 不应意外重排 next_run_at（diff 提交让调度语义精确）
        const body: CronJobUpdate = {}
        if (v.name.trim() !== editing.name) body.name = v.name.trim()
        if (v.prompt !== editing.prompt) body.prompt = v.prompt
        if (v.interval_minutes !== editing.interval_minutes) body.interval_minutes = v.interval_minutes
        if (v.start_mode === 'custom') body.start_at = startAt
        if (!Object.keys(body).length) {
          setOpen(false)
          return
        }
        await updateJob.mutateAsync({ id: editing.id, body })
      } else {
        await createJob.mutateAsync({
          user_id: userId,
          agent_id: v.agent_id,
          name: v.name.trim(),
          prompt: v.prompt,
          interval_minutes: v.interval_minutes,
          // 创建：null=立即首跑（下个 tick）；自定义=该时刻首跑
          start_at: v.start_mode === 'custom' ? startAt : null,
        })
      }
      setOpen(false)
    } catch {
      /* 错误由全局提示（后端中文 detail：任务名冲突 409 / 间隔越界 422 / agent 类型 422） */
    }
  }

  const handleToggle = (job: CronJobOut, enabled: boolean) => {
    updateJob.mutate({ id: job.id, body: { enabled } })
  }

  const handleRunNow = async (job: CronJobOut) => {
    try {
      await runNow.mutateAsync(job.id)
      // 立即触发后状态很快变化，提前失效一次列表（不等 30s 轮询）
      jobs.refetch()
    } catch {
      /* 409（运行中/待审批/会话占用）由全局提示 */
    }
  }

  const handleDelete = (job: CronJobOut) => {
    modal.confirm({
      title: `删除任务「${job.name}」？`,
      content: '运行历史一并删除；任务会话软归档保留，历史产出仍可在会话列表浏览。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: () => deleteJob.mutateAsync(job.id),
    })
  }

  const columns: ColumnsType<CronJobOut> = [
    {
      title: '任务名',
      dataIndex: 'name',
      key: 'name',
      render: (v: string, r) => (
        <Flex vertical gap={2}>
          <span>{v}</span>
          <Typography.Text
            type="secondary"
            style={{ fontSize: 12, maxWidth: 360 }}
            ellipsis={{ tooltip: r.prompt }}
          >
            {r.prompt}
          </Typography.Text>
        </Flex>
      ),
    },
    { title: '智能体', dataIndex: 'agent_name', key: 'agent_name', width: 130, render: (v: string | null) => v ?? '—' },
    { title: '间隔', dataIndex: 'interval_minutes', key: 'interval', width: 100, render: (v: number) => <Tag>{intervalLabel(v)}</Tag> },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, r) => {
        if (!r.enabled) return <Tag color="default">已停用</Tag>
        const t = STATUS_TAG[r.last_status ?? 'ok'] ?? { color: 'default', text: r.last_status ?? '—' }
        const tag = <Tag color={t.color}>{t.text}</Tag>
        // 熔断/异常详情经 Tooltip 透出（last_error 含后端写入的原因说明）
        return r.last_error ? (
          <Tooltip title={r.last_error} placement="topLeft">
            {tag}
          </Tooltip>
        ) : (
          tag
        )
      },
    },
    {
      title: '下次运行',
      dataIndex: 'next_run_at',
      key: 'next_run_at',
      width: 150,
      render: (v: string | null) =>
        v ? <Tooltip title={formatDateTime(v)}>{formatRelative(v)}</Tooltip> : <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>,
    },
    {
      title: '上次运行',
      dataIndex: 'last_run_at',
      key: 'last_run_at',
      width: 150,
      render: (v: string | null) =>
        v ? <Tooltip title={formatDateTime(v)}>{formatRelative(v)}</Tooltip> : <span style={{ color: 'var(--color-text-tertiary)' }}>未运行</span>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 270,
      render: (_, r) => (
        <Flex gap={0} align="center" onClick={(e) => e.stopPropagation()}>
          <Tooltip title={r.enabled ? '停用' : '启用（一个间隔后首跑）'}>
            <Switch size="small" checked={r.enabled} onChange={(v) => handleToggle(r, v)} />
          </Tooltip>
          <Button type="link" size="small" icon={<Play size={14} />} onClick={() => handleRunNow(r)}>
            运行
          </Button>
          <Button type="link" size="small" icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions/${r.session_id}`)}>
            会话
          </Button>
          <Tooltip title="运行历史">
            <Button type="link" size="small" icon={<History size={14} />} onClick={() => setRunsFor(r)} />
          </Tooltip>
          <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(r)} />
          <Popconfirm title="删除该任务？" onConfirm={() => handleDelete(r)} okText="删除" cancelText="取消" okButtonProps={{ danger: true }}>
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    },
  ]

  // agent 下拉只列 cron 型 + active（kind 隔离的绑定面；后端 422 为本体，此处为 UX）
  const cronAgents = (agents.data ?? []).filter((a) => a.status === 'active' && a.kind === 'cron')
  const rows = jobs.data ?? []

  return (
    <PageContainer
      title="定时任务"
      subtitle="绑定 cron 型智能体，按间隔自动注入 prompt 运行；产出落任务会话可回溯"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} disabled={!cronAgents.length}>
          新建任务
        </Button>
      }
    >
      {jobs.isLoading ? (
        <PageLoading />
      ) : jobs.isError ? (
        <ErrorState onRetry={() => jobs.refetch()} />
      ) : !rows.length ? (
        <EmptyState
          title={`用户「${userId}」暂无定时任务`}
          description={
            cronAgents.length
              ? '点击「新建任务」绑定 cron 型智能体开始调度'
              : '请先在「智能体」中创建一个 cron 型（定时任务专用）智能体'
          }
          extra={
            cronAgents.length ? (
              <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
                新建任务
              </Button>
            ) : undefined
          }
        />
      ) : (
        <Table<CronJobOut>
          rowKey="id"
          columns={columns}
          dataSource={rows}
          pagination={listPager}
          size="middle"
        />
      )}

      <Modal
        title={editing ? `编辑任务：${editing.name}` : '新建定时任务'}
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSubmit}
        okText={editing ? '保存' : '创建'}
        cancelText="取消"
        confirmLoading={createJob.isPending || updateJob.isPending}
        destroyOnClose
        width={560}
      >
        <Form<JobFormValues> form={form} layout="vertical" initialValues={{ start_mode: 'now' }}>
          <Form.Item
            name="name"
            label="任务名"
            rules={[{ required: true, message: '请输入任务名' }, { max: 100, message: '最长 100 字符' }]}
          >
            <Input placeholder="如：每日行业资讯摘要" />
          </Form.Item>
          <Form.Item name="agent_id" label="智能体（cron 型）" rules={[{ required: true, message: '请选择智能体' }]}>
            <Select
              placeholder="选择 cron 型智能体"
              options={cronAgents.map((a) => ({ value: a.id, label: a.name }))}
              loading={agents.isLoading}
              disabled={!!editing}
            />
          </Form.Item>
          <Form.Item
            name="prompt"
            label="任务内容"
            rules={[{ required: true, message: '请输入任务内容（每次触发注入的固定指令）' }]}
          >
            <Input.TextArea rows={4} placeholder="如：检索今日 AI 领域重要新闻，汇总为一份简报写入工作区。" showCount />
          </Form.Item>
          <Form.Item
            name="interval_minutes"
            label="触发间隔"
            rules={[{ required: true, message: '请选择或输入间隔（分钟）' }]}
            extra="后端默认下限 5 分钟（防高频烧配额）；间隔相位以开始时间为锚"
          >
            <Select options={INTERVAL_OPTIONS} placeholder="选择常用间隔" showSearch allowClear={false} />
          </Form.Item>
          <Form.Item label="开始时间" required>
            <Flex gap={8}>
              <Form.Item name="start_mode" noStyle initialValue="now">
                <Select
                  style={{ width: 120 }}
                  options={[
                    { value: 'now', label: '立即开始' },
                    { value: 'custom', label: '自定义' },
                  ]}
                />
              </Form.Item>
              <Form.Item
                noStyle
                shouldUpdate={(prev, cur) => prev.start_mode !== cur.start_mode}
              >
                {({ getFieldValue }) =>
                  getFieldValue('start_mode') === 'custom' ? (
                    <Form.Item
                      name="start_at"
                      noStyle
                      rules={[{ required: true, message: '请选择开始时间' }]}
                    >
                      {/* 分钟粒度：与后端调度精度对齐；序列化为带时区偏移的 ISO 8601 */}
                      <DatePicker showTime={{ format: 'HH:mm' }} format="YYYY-MM-DD HH:mm" style={{ width: 220 }} />
                    </Form.Item>
                  ) : (
                    <Typography.Text type="secondary" style={{ lineHeight: '32px' }}>
                      {editing ? '默认不改调度（仅显式选时间才重锚）' : '创建后下个调度周期（≤30s）内首跑'}
                    </Typography.Text>
                  )
                }
              </Form.Item>
            </Flex>
          </Form.Item>
        </Form>
      </Modal>

      <CronRunsDrawer job={runsFor} onClose={() => setRunsFor(null)} />
    </PageContainer>
  )
}

/** 运行历史 Drawer：每行一次执行（scheduled/manual + 状态 + 起止 + 错误） */
function CronRunsDrawer({ job, onClose }: { job: CronJobOut | null; onClose: () => void }): ReactNode {
  const runs = useCronRuns(job?.id ?? null)
  const rows: CronRunOut[] = runs.data ?? []

  const columns: ColumnsType<CronRunOut> = [
    { title: '开始', dataIndex: 'started_at', key: 'started_at', width: 150, render: formatDateTime },
    { title: '结束', dataIndex: 'finished_at', key: 'finished_at', width: 150, render: (v: string | null) => (v ? formatDateTime(v) : '—') },
    {
      title: '触发',
      dataIndex: 'trigger',
      key: 'trigger',
      width: 80,
      render: (v: string) => (v === 'manual' ? <Tag color="blue">手动</Tag> : <Tag>调度</Tag>),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) => {
        const t = RUN_STATUS_TAG[v] ?? { color: 'default', text: v }
        return <Tag color={t.color}>{t.text}</Tag>
      },
    },
    {
      title: '错误',
      dataIndex: 'error',
      key: 'error',
      ellipsis: { showTitle: true },
      render: (v: string | null) =>
        v ? (
          <Typography.Text type="danger" style={{ fontSize: 12 }} ellipsis={{ tooltip: v }}>
            {v}
          </Typography.Text>
        ) : (
          '—'
        ),
    },
  ]

  return (
    <Drawer
      title={
        job ? (
          <Flex align="center" gap={8}>
            <CalendarClock size={16} />
            <span>运行历史：{job.name}</span>
          </Flex>
        ) : (
          '运行历史'
        )
      }
      open={!!job}
      onClose={onClose}
      width={720}
      destroyOnClose
    >
      {runs.isLoading ? (
        <PageLoading />
      ) : runs.isError ? (
        <ErrorState onRetry={() => runs.refetch()} />
      ) : !rows.length ? (
        <EmptyState title="暂无运行记录" description="任务触发后此处显示每次执行的起止与结果" />
      ) : (
        <Table<CronRunOut> rowKey="id" columns={columns} dataSource={rows} pagination={false} size="small" />
      )}
    </Drawer>
  )
}
