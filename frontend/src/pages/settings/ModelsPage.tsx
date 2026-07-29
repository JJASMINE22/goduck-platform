/* 模型管理页（DESIGN §10.6）—— 注册表的增删改查 + 保存前 /test 连通探测。
   api_key 列表永不返回；编辑时留空 = 保留旧 key（轮换密钥）。
   删除被引用模型 → 后端 409 含 referencing_agents，弹窗提示先解绑。 */
import { App as AntApp, Button, Drawer, Flex, Form, Input, InputNumber, Popconfirm, Select, Switch, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { Eye, Pencil, Plug, Plus, Trash2 } from 'lucide-react'
import {
  useCreateModel,
  useDeleteModel,
  useModels,
  useTestModel,
  useUpdateModel,
} from '@/hooks/useModels'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { ApiError } from '@/api/client'
import { formatDateTime } from '@/lib/format'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import type { ModelInUseDetail, ModelOut, ProviderKind } from '@/types'

interface FormValues {
  name: string
  provider_kind: ProviderKind
  model_type: string
  api_key?: string
  base_url?: string
  supports_vision: boolean
  temperature?: number | null
  top_p?: number | null
  enable_thinking: boolean
  enabled: boolean
}

const emptyForm: FormValues = {
  name: '',
  provider_kind: 'openai_compatible',
  model_type: '',
  api_key: '',
  base_url: '',
  supports_vision: false,
  temperature: null,
  top_p: null,
  enable_thinking: false,
  enabled: true,
}

export function ModelsPage(): ReactNode {
  const { data, isLoading, isError, refetch } = useModels()
  const createModel = useCreateModel()
  const updateModel = useUpdateModel()
  const deleteModel = useDeleteModel()
  const testModel = useTestModel()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<ModelOut | null>(null)
  const [form] = Form.useForm<FormValues>()

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue(emptyForm)
    setOpen(true)
  }

  const openEdit = (record: ModelOut) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name,
      provider_kind: 'openai_compatible',
      model_type: record.model_type,
      api_key: '', // 安全：不回填，留空保留
      base_url: record.base_url ?? '',
      supports_vision: record.supports_vision,
      temperature: record.temperature ?? null,
      top_p: record.top_p ?? null,
      enable_thinking: record.enable_thinking,
      enabled: record.enabled,
    })
    setOpen(true)
  }

  const handleTest = async () => {
    try {
      const v = await form.validateFields(['provider_kind', 'model_type', 'api_key', 'base_url'])
      if (!v.api_key) {
        toastError('请先填写 api_key 再测试')
        return
      }
      const res = await testModel.mutateAsync({
        provider_kind: v.provider_kind,
        model_type: v.model_type,
        api_key: v.api_key,
        base_url: v.base_url || null,
      })
      if (res.ok) toastSuccess(`连接成功（${res.model_type}）`)
      else toastError(`连接失败：${res.error ?? '未知'}`)
    } catch {
      // validateFields 失败：表单自身已展示
    }
  }

  const handleSave = async () => {
    try {
      const v = await form.validateFields()
      if (editing) {
        await updateModel.mutateAsync({
          id: editing.id,
          body: { ...v, api_key: v.api_key ? v.api_key : null },
        })
      } else {
        await createModel.mutateAsync({ ...v, api_key: v.api_key ?? '' })
      }
      setOpen(false)
    } catch {
      // 校验或接口失败：接口错误由全局/hook 提示
    }
  }

  const handleDelete = (record: ModelOut) => {
    modal.confirm({
      title: `删除模型「${record.name}」？`,
      content: '删除后不可恢复。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteModel.mutateAsync(record.id)
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            const detail = e.detail as ModelInUseDetail | string
            const refs =
              typeof detail === 'object' && detail?.referencing_agents
                ? detail.referencing_agents
                : []
            modal.warning({
              title: '无法删除：仍有智能体在引用',
              content: (
                <div>
                  <p>{typeof detail === 'object' ? detail.message : '请先为以下智能体更换模型'}</p>
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

  const columns: ColumnsType<ModelOut> = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '模型型号', dataIndex: 'model_type', key: 'model_type' },
    {
      title: '视觉',
      dataIndex: 'supports_vision',
      key: 'supports_vision',
      width: 80,
      render: (v: boolean) => (v ? <Tag color="blue">支持</Tag> : <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>),
    },
    {
      title: '思考',
      dataIndex: 'enable_thinking',
      key: 'enable_thinking',
      width: 70,
      render: (v: boolean) => (v ? <Tag color="purple">开</Tag> : <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 80,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag>禁用</Tag>),
    },
    {
      title: '端点',
      dataIndex: 'base_url',
      key: 'base_url',
      ellipsis: true,
      render: (v: string | null) =>
        v ? <code style={{ fontSize: 12 }}>{v}</code> : <span style={{ color: 'var(--color-text-tertiary)' }}>原生 OpenAI</span>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (v: string) => formatDateTime(v),
    },
    {
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Flex gap={4}>
          <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(record)} />
          <Popconfirm title="删除该模型？" onConfirm={() => handleDelete(record)} okText="删除" cancelText="取消">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    },
  ]

  const busy = createModel.isPending || updateModel.isPending || testModel.isPending

  return (
    <PageContainer
      title="模型管理"
      subtitle="注册 OpenAI 兼容模型（通吃 OpenAI / DeepSeek / Qwen / 本地）。智能体按 model_id 绑定。"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
          注册模型
        </Button>
      }
    >
      {isLoading ? (
        <PageLoading />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState
          title="暂无模型"
          description="注册一个 OpenAI 兼容模型后即可创建智能体"
          extra={
            <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
              注册模型
            </Button>
          }
        />
      ) : (
        <Table<ModelOut>
          rowKey="id"
          columns={columns}
          dataSource={data}
          pagination={false}
          size="middle"
        />
      )}

      <Drawer
        title={editing ? `编辑模型：${editing.name}` : '注册模型'}
        open={open}
        onClose={() => setOpen(false)}
        width={480}
        destroyOnClose
        extra={
          <Tooltip title="保存前测试连通性（不落库）">
            <Button icon={<Plug size={14} />} loading={testModel.isPending} onClick={handleTest}>
              测试连接
            </Button>
          </Tooltip>
        }
        footer={
          <Flex justify="flex-end" gap={8}>
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button type="primary" loading={busy} onClick={handleSave}>
              保存
            </Button>
          </Flex>
        }
      >
        <Form<FormValues> form={form} layout="vertical" initialValues={emptyForm}>
          <Form.Item name="name" label="展示名" rules={[{ required: true, message: '请输入展示名' }]}>
            <Input placeholder="如 我的OpenAI" />
          </Form.Item>
          <Form.Item name="provider_kind" label="客户端族">
            <Select
              options={[{ value: 'openai_compatible', label: 'OpenAI 兼容' }]}
              disabled
            />
          </Form.Item>
          <Form.Item name="model_type" label="模型型号" rules={[{ required: true, message: '请输入模型型号' }]}>
            <Input placeholder="如 gpt-4o-mini" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            rules={editing ? [] : [{ required: true, message: '请输入 api_key' }]}
            extra={editing ? '留空表示保留旧 key（轮换密钥时填新值）' : undefined}
          >
            <Input.Password
              autoComplete="new-password"
              placeholder={editing ? '留空保留旧 key' : 'sk-...'}
              prefix={<Eye size={14} />}
            />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" extra="OpenAI 兼容端点；原生 OpenAI 留空">
            <Input placeholder="如 https://api.deepseek.com/v1" />
          </Form.Item>
          <Flex gap={24}>
            <Form.Item name="temperature" label="温度（temperature）" extra="0–2，留空=默认">
              <InputNumber min={0} max={2} step={0.1} style={{ width: 150 }} placeholder="默认" />
            </Form.Item>
            <Form.Item name="top_p" label="Top P" extra="0–1，留空=默认">
              <InputNumber min={0} max={1} step={0.05} style={{ width: 150 }} placeholder="默认" />
            </Form.Item>
          </Flex>
          <Form.Item
            name="enable_thinking"
            label="思考模式"
            valuePropName="checked"
            extra="开启后注入 thinking 请求体（仅思考模型有效）"
          >
            <Switch />
          </Form.Item>
          <Flex gap={24}>
            <Form.Item name="supports_vision" label="多模态（视觉）" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Flex>
        </Form>
      </Drawer>
    </PageContainer>
  )
}
