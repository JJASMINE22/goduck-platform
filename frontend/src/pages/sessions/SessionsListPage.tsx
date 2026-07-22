/* 会话列表页 —— 按 user_id 列出会话（个人工具，user_id 可切换，默认 admin）。
   新建会话需选智能体；点行进入聊天。 */
import { App as AntApp, Button, Flex, Form, Input, Modal, Select, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { MessageSquare, Plus, Trash2 } from 'lucide-react'
import { useSessions, useCreateSession, useArchiveSession } from '@/hooks/useSessions'
import { useAgents } from '@/hooks/useAgents'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatRelative } from '@/lib/format'
import type { SessionOut } from '@/types'

const USER_KEY = 'goduck.user_id'

export function SessionsListPage(): ReactNode {
  const navigate = useNavigate()
  const [userId, setUserId] = useState(() => localStorage.getItem(USER_KEY) || 'admin')
  const sessions = useSessions(userId)
  const agents = useAgents()
  const createSession = useCreateSession()
  const archiveSession = useArchiveSession()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<{ agent_id: string; title?: string }>()

  const changeUser = (v: string) => {
    setUserId(v)
    localStorage.setItem(USER_KEY, v)
  }

  const handleCreate = async () => {
    const v = await form.validateFields()
    const session = await createSession.mutateAsync({
      user_id: userId,
      agent_id: v.agent_id,
      title: v.title || null,
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
      render: (v: string) => (v === 'active' ? <Tag color="green">活跃</Tag> : <Tag>归档</Tag>),
    },
    { title: '更新', dataIndex: 'updated_at', key: 'updated_at', width: 140, render: formatRelative },
    {
      title: '操作',
      key: 'actions',
      width: 130,
      render: (_, r) => (
        <Flex gap={4}>
          <Button type="link" size="small" icon={<MessageSquare size={14} />} onClick={() => navigate(`/sessions/${r.id}`)}>
            打开
          </Button>
          <Tooltip title="归档">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} onClick={() => handleArchive(r)} />
          </Tooltip>
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
        <Flex gap={8}>
          <Input
            size="middle"
            value={userId}
            onChange={(e) => changeUser(e.target.value)}
            addonBefore="用户"
            style={{ width: 200 }}
          />
          <Button type="primary" icon={<Plus size={16} />} onClick={() => setOpen(true)} disabled={!activeAgents.length}>
            新建会话
          </Button>
        </Flex>
      }
    >
      {sessions.isLoading ? (
        <PageLoading />
      ) : sessions.isError ? (
        <ErrorState onRetry={() => sessions.refetch()} />
      ) : !sessions.data?.length ? (
        <EmptyState
          title={`用户「${userId}」暂无会话`}
          description={activeAgents.length ? '点击「新建会话」开始对话' : '请先在「智能体」中创建一个 active 智能体'}
        />
      ) : (
        <Table<SessionOut>
          rowKey="id"
          columns={columns}
          dataSource={sessions.data}
          pagination={{ pageSize: 15 }}
          size="middle"
          onRow={(r) => ({ onClick: () => navigate(`/sessions/${r.id}`), style: { cursor: 'pointer' } })}
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
              placeholder="选择一个 active 智能体"
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
