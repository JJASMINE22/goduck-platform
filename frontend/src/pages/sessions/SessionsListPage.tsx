/* 会话列表页 —— 按 user_id 列出会话（个人工具；user_id 由顶栏统一切换，默认 admin）。
   新建会话需选智能体；点行进入聊天。 */
import { App as AntApp, Button, Flex, Form, Input, Modal, Popconfirm, Segmented, Select, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { MessageSquare, Plus, Trash2 } from 'lucide-react'
import { useSessions, useCreateSession, useArchiveSession, usePurgeSession } from '@/hooks/useSessions'
import { useAgents } from '@/hooks/useAgents'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatRelative } from '@/lib/format'
import type { SessionOut } from '@/types'

export function SessionsListPage(): ReactNode {
  const navigate = useNavigate()
  const userId = useCurrentUser((s) => s.userId)
  const [showArchived, setShowArchived] = useState(false)
  const sessions = useSessions(userId, showArchived)
  const rows = showArchived
    ? (sessions.data ?? []).filter((s) => s.status === 'archived')
    : (sessions.data ?? [])
  const agents = useAgents()
  const createSession = useCreateSession()
  const archiveSession = useArchiveSession()
  const purgeSession = usePurgeSession()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<{ agent_id: string; title?: string }>()

  const handleCreate = async () => {
    const v = await form.validateFields()
    const session = await createSession.mutateAsync({
      user_id: userId,
      agent_id: v.agent_id,
      // 空白标题归一为未命名（后端同名校验仅对非空标题生效）
      title: v.title?.trim() || null,
    })
    setOpen(false)
    form.resetFields()
    navigate(`/sessions/${session.id}`)
  }

  const handleArchive = (record: SessionOut) => {
    modal.confirm({
      title: '归档该会话？',
      content: '归档后无法继续对话。',
      okType: 'danger',
      okText: '归档',
      cancelText: '取消',
      onOk: () => archiveSession.mutateAsync(record.id),
    })
  }

  const handlePurge = (record: SessionOut) => {
    modal.confirm({
      title: '彻底删除该会话？',
      content: '将永久删除会话及其全部对话与 checkpoint，不可恢复。',
      okType: 'danger',
      okText: '彻底删除',
      cancelText: '取消',
      onOk: () => purgeSession.mutateAsync(record.id),
    })
  }

  const columns: ColumnsType<SessionOut> = [
    {
      title: '标题',
      dataIndex: 'title',
      key: 'title',
      render: (v: string | null, r) => v || <span style={{ color: 'var(--color-text-tertiary)' }}>未命名（{r.id.slice(0, 8)}）</span>,
    },
    { title: '智能体', dataIndex: 'agent_name', key: 'agent_name', render: (v: string | null) => v ?? '—' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (v: string) => (v === 'active' ? <Tag color="green">活跃</Tag> : <Tag>已归档</Tag>),
    },
    { title: '更新', dataIndex: 'updated_at', key: 'updated_at', width: 140, render: formatRelative },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, r) => (
        <Flex gap={4} onClick={(e) => e.stopPropagation()}>
          <Button
            type="link"
            size="small"
            icon={<MessageSquare size={14} />}
            onClick={() => navigate(`/sessions/${r.id}`)}
          >
            {r.status === 'active' ? '打开' : '查看'}
          </Button>
          {r.status === 'archived' ? (
            <Popconfirm title="彻底删除？此操作不可恢复" onConfirm={() => handlePurge(r)} okText="彻底删除" cancelText="取消" okButtonProps={{ danger: true }}>
              <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
            </Popconfirm>
          ) : (
            <Tooltip title="归档">
              <Button type="link" size="small" danger icon={<Trash2 size={14} />} onClick={() => handleArchive(r)} />
            </Tooltip>
          )}
        </Flex>
      ),
    },
  ]

  const activeAgents = (agents.data ?? []).filter((a) => a.status === 'active')

  return (
    <PageContainer
      title="会话"
      subtitle="选择智能体开启对话；会话绑定 thread_id（LangGraph checkpointer 键）"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={() => setOpen(true)} disabled={!activeAgents.length}>
          新建会话
        </Button>
      }
    >
      <Segmented
        value={showArchived ? 'archived' : 'active'}
        onChange={(v) => setShowArchived(v === 'archived')}
        options={[
          { label: '活跃', value: 'active' },
          { label: '已归档', value: 'archived' },
        ]}
        style={{ marginBottom: 12 }}
      />
      {sessions.isLoading ? (
        <PageLoading />
      ) : sessions.isError ? (
        <ErrorState onRetry={() => sessions.refetch()} />
      ) : !rows.length ? (
        <EmptyState
          title={`用户「${userId}」暂无会话`}
          description={activeAgents.length ? '点击「新建会话」开始对话' : '请先在「智能体」中创建一个可用（活跃）智能体'}
        />
      ) : (
        <Table<SessionOut>
          rowKey="id"
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 15 }}
          size="middle"
          onRow={(r) => ({
            onClick: () => navigate(`/sessions/${r.id}`),
            style: { cursor: 'pointer' },
          })}
        />
      )}

      <Modal
        title="新建会话"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleCreate}
        okText="创建并进入"
        cancelText="取消"
        confirmLoading={createSession.isPending}
        destroyOnClose
      >
        <Form<{ agent_id: string; title?: string }> form={form} layout="vertical">
          <Form.Item name="agent_id" label="智能体" rules={[{ required: true, message: '请选择智能体' }]}>
            <Select
              placeholder="选择一个可用（活跃）智能体"
              options={activeAgents.map((a) => ({ value: a.id, label: a.name }))}
              loading={agents.isLoading}
            />
          </Form.Item>
          <Form.Item name="title" label="标题（可选）">
            <Input placeholder="如：需求评审助手对话" />
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  )
}
