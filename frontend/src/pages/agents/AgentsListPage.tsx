/* 智能体列表页 —— 列出所有智能体（draft/active/archived）。
   新建/编辑跳转编辑器；归档；导出 YAML 下载。 */
import { App as AntApp, Button, Flex, Popconfirm, Segmented, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Download, Pencil, Plus, Trash2 } from 'lucide-react'
import { agentsApi } from '@/api/agents'
import { useAgents, useArchiveAgent, usePurgeAgent } from '@/hooks/useAgents'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { AgentTopologyHover } from '@/features/topology/AgentTopologyHover'
import { formatRelative, shortId } from '@/lib/format'
import { toastSuccess } from '@/lib/antdStatic'
import type { AgentOut } from '@/types'

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  active: { color: 'green', text: 'active' },
  draft: { color: 'default', text: 'draft' },
  archived: { color: 'red', text: 'archived' },
}

export function AgentsListPage(): ReactNode {
  const navigate = useNavigate()
  const [showArchived, setShowArchived] = useState(false)
  const { data, isLoading, isError, refetch } = useAgents(showArchived)
  const archiveAgent = useArchiveAgent()
  const purgeAgent = usePurgeAgent()
  const { modal } = AntApp.useApp()

  const handleExport = async (record: AgentOut) => {
    try {
      const yaml = await agentsApi.exportYaml(record.id)
      const blob = new Blob([yaml], { type: 'text/yaml;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${record.name}.yaml`
      a.click()
      URL.revokeObjectURL(url)
      toastSuccess('已导出 YAML')
    } catch {
      // 错误由全局提示
    }
  }

  const handleArchive = (record: AgentOut) => {
    modal.confirm({
      title: `归档智能体「${record.name}」？`,
      content: '归档后不会删除配置与历史，但不可用于新会话。',
      okType: 'danger',
      okText: '归档',
      cancelText: '取消',
      onOk: () => archiveAgent.mutateAsync(record.id),
    })
  }

  const handlePurge = (record: AgentOut) => {
    modal.confirm({
      title: `彻底删除智能体「${record.name}」？`,
      content: '将永久删除配置、历史、全部会话与 checkpoint，不可恢复。',
      okType: 'danger',
      okText: '彻底删除',
      cancelText: '取消',
      onOk: () => purgeAgent.mutateAsync(record.id),
    })
  }

  const columns: ColumnsType<AgentOut> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (v: string, r) => (
        <AgentTopologyHover id={r.id}>
          <a onClick={() => navigate(`/agents/${r.id}`)}>{v}</a>
        </AgentTopologyHover>
      ),
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (v: string | null) => v || <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: string) => {
        const t = STATUS_TAG[v] ?? { color: 'default', text: v }
        return <Tag color={t.color}>{t.text}</Tag>
      },
    },
    {
      title: '绑定模型',
      dataIndex: 'model_id',
      key: 'model_id',
      width: 120,
      render: (v: string | null) => (v ? <code style={{ fontSize: 12 }}>{shortId(v)}</code> : <span style={{ color: 'var(--color-text-tertiary)' }}>未绑定</span>),
    },
    { title: '更新', dataIndex: 'updated_at', key: 'updated_at', width: 140, render: formatRelative },
    {
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, r) => (
        <Flex gap={4}>
          <Tooltip title="编辑">
            <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => navigate(`/agents/${r.id}`)} />
          </Tooltip>
          <Tooltip title="导出 YAML">
            <Button type="link" size="small" icon={<Download size={14} />} onClick={() => handleExport(r)} />
          </Tooltip>
          {r.status === 'archived' ? (
            <Popconfirm title="彻底删除？此操作不可恢复" onConfirm={() => handlePurge(r)} okText="彻底删除" cancelText="取消" okButtonProps={{ danger: true }}>
              <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
            </Popconfirm>
          ) : (
            <Popconfirm title="归档该智能体？" onConfirm={() => handleArchive(r)} okText="归档" cancelText="取消">
              <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
            </Popconfirm>
          )}
        </Flex>
      ),
    },
  ]

  return (
    <PageContainer
      title="智能体"
      subtitle="个人智能体搭建 / 迭代；更新即替换（旧会话级联删除，配置历史可还原）"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={() => navigate('/agents/new')}>
          新建智能体
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
      {isLoading ? (
        <PageLoading />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState
          title="暂无智能体"
          description="新建一个智能体，配置 system_prompt / 工具 / 子代理"
          extra={
            <Button type="primary" icon={<Plus size={16} />} onClick={() => navigate('/agents/new')}>
              新建智能体
            </Button>
          }
        />
      ) : (
        <Table<AgentOut> rowKey="id" columns={columns} dataSource={data} pagination={false} size="middle" />
      )}
    </PageContainer>
  )
}
