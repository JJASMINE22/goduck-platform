/* 智能体列表页 —— 列出所有智能体（draft/active/archived）。
   新建/编辑/归档/彻底删除为管理员操作（后端 403 兜底）；非管理员可浏览
   列表与悬浮预览架构、导出 YAML（只读面）。 */
import { App as AntApp, Button, Flex, Popconfirm, Segmented, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Download, Pencil, Plus, Trash2 } from 'lucide-react'
import { agentsApi } from '@/api/agents'
import { useAgents, useArchiveAgent, usePurgeAgent } from '@/hooks/useAgents'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { AgentTopologyHover } from '@/features/topology/AgentTopologyHover'
import { formatRelative, shortId } from '@/lib/format'
import { toastSuccess } from '@/lib/antdStatic'
import type { AgentOut } from '@/types'

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  active: { color: 'green', text: '活跃' },
  draft: { color: 'default', text: '草稿' },
  archived: { color: 'red', text: '已归档' },
}

export function AgentsListPage(): ReactNode {
  const navigate = useNavigate()
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)
  const [showArchived, setShowArchived] = useState(false)
  const { data, isLoading, isError, refetch } = useAgents(showArchived)
  // 后端 include_archived 是「是否把归档项并入」，而非「仅看归档」；
  // 故在客户端筛选为仅归档，与会话页语义保持一致。
  const rows = showArchived ? (data ?? []).filter((a) => a.status === 'archived') : data ?? []
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
          {/* 非管理员不进编辑器：名称只读展示（架构预览走悬浮卡） */}
          {isManager ? (
            <a onClick={() => navigate(`/agents/${r.id}`)}>{v}</a>
          ) : (
            <span style={{ fontWeight: 600 }}>{v}</span>
          )}
        </AgentTopologyHover>
      ),
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      align: 'left',
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
      title: '用途',
      dataIndex: 'kind',
      key: 'kind',
      width: 100,
      render: (v: string | undefined) =>
        v === 'cron' ? <Tag color="geekblue">定时任务</Tag> : <Tag color="green">聊天会话</Tag>,
    },
    {
      title: '绑定模型',
      dataIndex: 'model_name',
      key: 'model_name',
      width: 160,
      ellipsis: true,
      render: (v: string | null, r) => {
        // 优先名称（后端 JOIN）；名称缺失（旧数据/变更响应）回退短 id，未绑定显示占位
        if (v) return <span title={v}>{v}</span>
        if (r.model_id) return <code style={{ fontSize: 12 }}>{shortId(r.model_id)}</code>
        return <span style={{ color: 'var(--color-text-tertiary)' }}>未绑定</span>
      },
    },
    { title: '更新', dataIndex: 'updated_at', key: 'updated_at', width: 140, render: formatRelative },
  ]
  // 操作列仅管理员渲染（编辑/导出/归档/删除整列对普通用户隐藏）
  if (isManager) {
    columns.push({
      title: '操作',
      key: 'actions',
      width: 150,
      render: (_, r) => (
        <Flex gap={4} justify="center">
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
    })
  }

  return (
    <PageContainer
      title="智能体列表"
      subtitle="搭建 / 迭代个人智能体；更新即替换，配置历史可还原"
      extra={
        isManager ? (
          <Button type="primary" icon={<Plus size={16} />} onClick={() => navigate('/agents/new')}>
            新建智能体
          </Button>
        ) : null
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
      ) : !rows.length ? (
        showArchived ? (
          <EmptyState title="暂无已归档智能体" description="归档后的智能体会显示在此处，可彻底删除或保留历史。" />
        ) : (
          <EmptyState
            title="暂无智能体"
            description={
              isManager
                ? '新建一个智能体，配置 system_prompt / 工具 / 子代理'
                : '暂无可用智能体，请联系管理员创建'
            }
            extra={
              isManager ? (
                <Button type="primary" icon={<Plus size={16} />} onClick={() => navigate('/agents/new')}>
                  新建智能体
                </Button>
              ) : null
            }
          />
        )
      ) : (
        <Table<AgentOut> rowKey="id" columns={columns} dataSource={rows} pagination={false} size="middle" />
      )}
    </PageContainer>
  )
}
