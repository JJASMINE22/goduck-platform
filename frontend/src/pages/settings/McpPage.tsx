/* MCP 运维页（DESIGN §10）—— server CRUD（触发后端 refresh）+ 工具按 server 行展开 + 健康状态 + 强制重连。
   config 是动态 JSON：stdio={command,args,env} | http={url,headers}。表单用 JSON 文本编辑。 */
import {
  App as AntApp,
  Button,
  Drawer,
  Flex,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Pencil, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import {
  useCreateMcpServer,
  useDeleteMcpServer,
  useMcpServers,
  useMcpStatus,
  useMcpTools,
  useRefreshMcp,
  useUpdateMcpServer,
} from '@/hooks/useMcp'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatDateTime } from '@/lib/format'
import { toastError } from '@/lib/antdStatic'
import { ApiError } from '@/api/client'
import type { McpInUseDetail, McpKind, McpServerOut, ToolInfo } from '@/types'

interface FormValues {
  name: string
  kind: McpKind
  configText: string
  enabled: boolean
}

const templates: Record<McpKind, string> = {
  http: JSON.stringify({ url: 'http://127.0.0.1:8088/mcp' }, null, 2),
  streamable_http: JSON.stringify(
    {
      url: 'https://connect.patsnap.com/mcp/patsnap-search?apikey=YOUR_PATSNAP_API_KEY',
      headers: {},
    },
    null,
    2,
  ),
  stdio: JSON.stringify({ command: 'python', args: ['-m', 'some_mcp'], env: {} }, null, 2),
}

/** kind 元数据（表格 Tag 与表单 Select 共用；列宽按最长 label 定尺寸） */
const kindMeta: Record<McpKind, { label: string; color: string }> = {
  http: { label: 'HTTP', color: 'blue' },
  streamable_http: { label: 'Streamable HTTP', color: 'cyan' },
  stdio: { label: 'STDIO', color: 'purple' },
}

const emptyForm: FormValues = { name: '', kind: 'http', configText: templates.http, enabled: true }

/** 去掉 "{server}__" 前缀的工具短名 */
function shortToolName(fullName: string): string {
  const idx = fullName.indexOf('__')
  return idx >= 0 ? fullName.slice(idx + 2) : fullName
}

export function McpPage(): ReactNode {
  const servers = useMcpServers()
  const tools = useMcpTools()
  const status = useMcpStatus()
  const createServer = useCreateMcpServer()
  const updateServer = useUpdateMcpServer()
  const deleteServer = useDeleteMcpServer()
  const refresh = useRefreshMcp()
  const { modal } = AntApp.useApp()

  // 按 server 名建立健康状态索引，供表格"状态"列 join（/mcp/servers 行 ↔ /mcp/status 明细）
  const statusMap = useMemo(
    () => new Map((status.data?.servers ?? []).map((s) => [s.name, s])),
    [status.data],
  )

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<McpServerOut | null>(null)
  const [form] = Form.useForm<FormValues>()

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue(emptyForm)
    setOpen(true)
  }

  const openEdit = (record: McpServerOut) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name,
      kind: record.kind as McpKind,
      configText: JSON.stringify(record.config_json, null, 2),
      enabled: record.enabled,
    })
    setOpen(true)
  }

  const handleSave = async () => {
    try {
      const v = await form.validateFields()
      let config: Record<string, unknown>
      try {
        config = JSON.parse(v.configText)
      } catch {
        toastError('config 不是合法 JSON')
        return
      }
      const body = { name: v.name, kind: v.kind, config, enabled: v.enabled }
      if (editing) await updateServer.mutateAsync({ id: editing.id, body })
      else await createServer.mutateAsync(body)
      setOpen(false)
    } catch {
      // 校验/接口失败：接口错误由全局提示
    }
  }

  const handleDelete = (record: McpServerOut) => {
    modal.confirm({
      title: `删除 MCP server「${record.name}」？`,
      content: '删除后将触发重连，引用该 server 工具的智能体需重新配置。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteServer.mutateAsync(record.id)
        } catch (e) {
          // 409=仍有智能体引用其工具（后端按引用阻断）：列出引用方供手动解绑
          if (e instanceof ApiError && e.status === 409) {
            const detail = e.detail as McpInUseDetail | string
            const refs =
              typeof detail === 'object' && detail?.referencing_agents
                ? detail.referencing_agents
                : []
            modal.warning({
              title: '无法删除：仍有智能体在引用',
              content: (
                <div>
                  <p>{typeof detail === 'object' ? detail.message : '请先在以下智能体中移除相关工具'}</p>
                  {refs.length ? (
                    <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                      {refs.map((r) => (
                        <li key={r.id}>
                          {r.name} <code style={{ fontSize: 12 }}>{r.id.slice(0, 8)}</code>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ),
            })
          }
          // 其它错误由全局兜底提示
        }
      },
    })
  }

  const serverCols: ColumnsType<McpServerOut> = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true, render: (v: string) => <code>{v}</code> },
    {
      title: '类型',
      dataIndex: 'kind',
      key: 'kind',
      width: 160,
      render: (v: string) => {
        const meta = kindMeta[v as McpKind]
        return <Tag color={meta?.color}>{meta?.label ?? v}</Tag>
      },
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: (v: boolean) => (v ? <Tag color="green">是</Tag> : <Tag>否</Tag>),
    },
    {
      title: '状态',
      key: 'health',
      width: 140,
      render: (_, r) => {
        const s = statusMap.get(r.name)
        if (!s) return <Tag>未知</Tag>
        return s.healthy ? (
          <Tag color="green">正常 · {s.tool_count} 工具</Tag>
        ) : (
          <Tooltip title={s.error ?? '连接失败'} placement="top">
            <Tag color="red">异常</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '注册时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 200,
      ellipsis: true,
      render: formatDateTime,
    },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_, r) => (
        <Flex gap={4}>
          <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(r)} />
          <Popconfirm title="删除该 server？" onConfirm={() => handleDelete(r)} okText="删除" cancelText="取消">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    },
  ]

  // —— 工具随所属 server 行展开（server 列表与工具列表合并，消除重复） ——
  const [toolSearch, setToolSearch] = useState('')
  const [expandedKeys, setExpandedKeys] = useState<string[]>([])

  // 工具按命名空间分组：{server}__{tool} → 以首个 "__" 前的 server 名为 key
  const toolsByServer = useMemo(() => {
    const map = new Map<string, ToolInfo[]>()
    for (const t of tools.data ?? []) {
      const idx = t.name.indexOf('__')
      const ns = idx >= 0 ? t.name.slice(0, idx) : t.name
      const arr = map.get(ns)
      if (arr) arr.push(t)
      else map.set(ns, [t])
    }
    return map
  }, [tools.data])

  // 取某 server 命中搜索词的工具；无搜索词时返回该 server 全部工具
  const searchQ = toolSearch.trim().toLowerCase()
  const toolsForServer = useCallback(
    (serverName: string): ToolInfo[] => {
      const all = toolsByServer.get(serverName) ?? []
      if (!searchQ) return all
      return all.filter((t) => {
        const idx = t.name.indexOf('__')
        const short = idx >= 0 ? t.name.slice(idx + 2) : t.name
        return short.toLowerCase().includes(searchQ) || (t.description ?? '').toLowerCase().includes(searchQ)
      })
    },
    [toolsByServer, searchQ],
  )

  // 搜索态自动展开命中工具的 server 行；否则沿用用户手动展开
  const searching = searchQ.length > 0
  const expandedRowKeys = searching
    ? (servers.data ?? []).filter((s) => toolsForServer(s.name).length > 0).map((s) => s.id)
    : expandedKeys

  const busy = createServer.isPending || updateServer.isPending

  return (
    <PageContainer
      title="MCP 服务列表"
      subtitle="工具全 MCP 化；按 {server}__{tool} 命名防跨服务冲突"
      extra={
        <Space>
          <Button icon={<RefreshCw size={14} />} loading={refresh.isPending} onClick={() => refresh.mutate()}>
            强制重连
          </Button>
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
            注册服务
          </Button>
        </Space>
      }
    >
      {/* 状态卡 */}
      <Flex gap={16} wrap style={{ marginBottom: 16 }}>
        <Tag color={status.data?.healthy ? 'green' : 'red'} style={{ padding: '4px 12px' }}>
          {status.isLoading ? '检测中' : status.data?.healthy ? '连接正常' : '连接异常'}
        </Tag>
        <Typography.Text type="secondary">
          server 数：<b>{status.data?.server_count ?? '—'}</b>
        </Typography.Text>
        <Typography.Text type="secondary">
          工具数：<b>{status.data?.tool_count ?? tools.data?.length ?? '—'}</b>
        </Typography.Text>
      </Flex>

      {servers.isLoading ? (
        <PageLoading />
      ) : servers.isError ? (
        <ErrorState onRetry={() => servers.refetch()} />
      ) : !servers.data?.length ? (
        <EmptyState title="暂无 MCP server" extra={<Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>注册服务</Button>} />
      ) : (
        <>
          {/* 工具搜索：命中工具的所属 server 行自动展开 */}
          <Flex justify="flex-end" style={{ marginBottom: 12 }}>
            <Input
              allowClear
              prefix={<Search size={14} />}
              placeholder="搜索工具名 / 描述（自动展开命中服务）"
              value={toolSearch}
              onChange={(e) => setToolSearch(e.target.value)}
              style={{ maxWidth: 360 }}
            />
          </Flex>
          <Table<McpServerOut>
            rowKey="id"
            columns={serverCols}
            dataSource={servers.data}
            pagination={false}
            size="middle"
            tableLayout="fixed"
            scroll={{ x: 920 }}
            expandable={{
              expandedRowKeys,
              onExpandedRowsChange: (keys) => setExpandedKeys(keys as string[]),
              rowExpandable: () => true,
              expandedRowRender: (r) => {
                const matched = toolsForServer(r.name)
                const total = toolsByServer.get(r.name)?.length ?? 0
                return (
                  <div className="mcp-tools">
                    {searching && matched.length !== total && (
                      <div className="mcp-tools__meta">
                        命中 {matched.length} / {total} 个工具
                      </div>
                    )}
                    {matched.length ? (
                      <div className="mcp-tools__list">
                        {matched.map((t) => (
                          <div key={t.name} className="mcp-tool" title={t.description || undefined}>
                            <code className="mcp-tool__name">{shortToolName(t.name)}</code>
                            <span className="mcp-tool__desc">{t.description || '—'}</span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <Typography.Text type="secondary">
                        {tools.isLoading ? '工具加载中…' : '该服务暂无工具'}
                      </Typography.Text>
                    )}
                  </div>
                )
              },
            }}
          />
        </>
      )}

      <Drawer
        title={editing ? `编辑服务：${editing.name}` : '注册 MCP 服务'}
        open={open}
        onClose={() => setOpen(false)}
        width={520}
        destroyOnClose
        footer={
          <Flex justify="flex-end" gap={8}>
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button type="primary" loading={busy} onClick={handleSave}>保存</Button>
          </Flex>
        }
      >
        <Form<FormValues> form={form} layout="vertical" initialValues={emptyForm}>
          <Form.Item name="name" label="名称（工具名前缀）" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如 UnifiedGateway" />
          </Form.Item>
          <Form.Item name="kind" label="类型">
            <Select
              options={Object.entries(kindMeta).map(([value, m]) => ({ value, label: m.label }))}
              onChange={(k: McpKind) => form.setFieldValue('configText', templates[k])}
            />
          </Form.Item>
          <Form.Item
            name="configText"
            label="config（JSON）"
            rules={[{ required: true, message: '请填写 config' }]}
            extra="http / streamable_http: {url, headers}  ·  stdio: {command, args, env}"
          >
            <Input.TextArea rows={8} style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Drawer>
    </PageContainer>
  )
}
