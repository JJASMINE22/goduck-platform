/* 向量模型管理页 —— 注册表增删改查 + 保存前 /test 连通探测（实测维度）。
   api_key 列表永不返回；编辑时留空 = 保留旧 key。
   语义字段（端点/型号/维度）被知识库引用 → 后端 409 含 referencing_kbs，
   弹窗提示先换绑。镜像 ModelsPage。 */
import { App as AntApp, Button, Drawer, Flex, Form, Input, InputNumber, Popconfirm, Select, Switch, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { Pencil, Plug, Plus, Trash2 } from 'lucide-react'
import {
  useCreateEmbedding,
  useDeleteEmbedding,
  useEmbeddings,
  useTestEmbedding,
  useUpdateEmbedding,
} from '@/hooks/useEmbeddings'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { ApiError } from '@/api/client'
import { formatDateTime } from '@/lib/format'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import { useCurrentUser } from '@/store/userStore'
import type { EmbeddingOut, EmbeddingReferencedDetail, EmbeddingProviderKind } from '@/types'

interface FormValues {
  name: string
  provider_kind: EmbeddingProviderKind
  model_type: string
  api_key?: string
  base_url: string
  dims?: number | null
  batch_size: number
  timeout: number
  check_ctx_length: boolean
  enabled: boolean
}

const emptyForm: FormValues = {
  name: '',
  provider_kind: 'openai_compatible',
  model_type: '',
  api_key: '',
  base_url: '',
  dims: null,
  batch_size: 24,
  timeout: 90,
  check_ctx_length: false,
  enabled: true,
}

export function EmbeddingsPage(): ReactNode {
  // 写操作为管理员专属；非管理员只读预览
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)
  const { data, isLoading, isError, refetch } = useEmbeddings()
  const createEmbedding = useCreateEmbedding()
  const updateEmbedding = useUpdateEmbedding()
  const deleteEmbedding = useDeleteEmbedding()
  const testEmbedding = useTestEmbedding()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<EmbeddingOut | null>(null)
  const [form] = Form.useForm<FormValues>()

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue(emptyForm)
    setOpen(true)
  }

  const openEdit = (record: EmbeddingOut) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name,
      provider_kind: 'openai_compatible',
      model_type: record.model_type,
      api_key: '', // 安全：不回填，留空保留
      base_url: record.base_url,
      dims: record.dims ?? null,
      batch_size: record.batch_size,
      timeout: record.timeout,
      check_ctx_length: record.check_ctx_length,
      enabled: record.enabled,
    })
    setOpen(true)
  }

  const handleTest = async () => {
    try {
      const v = await form.validateFields(['model_type', 'api_key', 'base_url'])
      if (!v.api_key) {
        toastError('请先填写 api_key 再测试')
        return
      }
      const res = await testEmbedding.mutateAsync({
        model_type: v.model_type,
        api_key: v.api_key,
        base_url: v.base_url,
      })
      if (res.ok) toastSuccess(`连接成功（实测维度 ${res.dims}）`)
      else toastError(`连接失败：${res.error ?? '未知'}`)
    } catch {
      // validateFields 失败：表单自身已展示
    }
  }

  const handleSave = async () => {
    try {
      const v = await form.validateFields()
      if (editing) {
        await updateEmbedding.mutateAsync({
          id: editing.id,
          body: { ...v, api_key: v.api_key ? v.api_key : null },
        })
      } else {
        await createEmbedding.mutateAsync({ ...v, api_key: v.api_key ?? '' })
      }
      setOpen(false)
    } catch {
      // 校验或接口失败：接口错误由全局/hook 提示
    }
  }

  const handleDelete = (record: EmbeddingOut) => {
    modal.confirm({
      title: `删除向量模型「${record.name}」？`,
      content: '删除后不可恢复。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: async () => {
        try {
          await deleteEmbedding.mutateAsync(record.id)
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            const detail = e.detail as EmbeddingReferencedDetail | string
            const refs =
              typeof detail === 'object' && detail?.referencing_kbs ? detail.referencing_kbs : []
            modal.warning({
              title: '无法删除：仍有知识库在引用',
              content: (
                <div>
                  <p>{typeof detail === 'object' ? detail.message : '请先为以下知识库换绑向量模型'}</p>
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
        }
      },
    })
  }

  const columns: ColumnsType<EmbeddingOut> = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    { title: '型号', dataIndex: 'model_type', key: 'model_type', ellipsis: true },
    {
      title: '维度',
      dataIndex: 'dims',
      key: 'dims',
      width: 90,
      render: (v?: number | null) => (v != null ? <Tag color="geekblue">{v}</Tag> : '原生'),
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
      align: 'left',
      ellipsis: true,
      render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code>,
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (v: string) => formatDateTime(v),
    },
  ]
  if (isManager) {
    columns.push({
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Flex gap={4} justify="center">
          <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(record)} />
          <Popconfirm title="删除该向量模型？" onConfirm={() => handleDelete(record)} okText="删除" cancelText="取消">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    })
  }

  const busy = createEmbedding.isPending || updateEmbedding.isPending || testEmbedding.isPending

  return (
    <PageContainer
      title="向量化"
      subtitle="注册嵌入端点（TEI / OpenAI 兼容）；知识库按 embedding_id 绑定，维度随行"
      extra={
        isManager ? (
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
            注册向量模型
          </Button>
        ) : null
      }
    >
      {isLoading ? (
        <PageLoading />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState
          title="暂无向量模型"
          description="注册一个嵌入端点后即可在建库时绑定"
          extra={
            isManager ? (
              <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
                注册向量模型
              </Button>
            ) : null
          }
        />
      ) : (
        <Table<EmbeddingOut>
          rowKey="id"
          columns={columns}
          dataSource={data}
          pagination={false}
          size="middle"
        />
      )}

      <Drawer
        title={editing ? `编辑向量模型：${editing.name}` : '注册向量模型'}
        open={open}
        onClose={() => setOpen(false)}
        width={480}
        destroyOnClose
        extra={
          <Tooltip title="保存前测试连通性并实测维度（不落库）">
            <Button icon={<Plug size={14} />} loading={testEmbedding.isPending} onClick={handleTest}>
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
            <Input placeholder="如 Qwen3-Embedding-0.6B（TEI 内置）" />
          </Form.Item>
          <Form.Item name="provider_kind" label="客户端族">
            <Select options={[{ value: 'openai_compatible', label: 'OpenAI 兼容' }]} disabled />
          </Form.Item>
          <Form.Item
            name="model_type"
            label="模型型号"
            rules={[{ required: true, message: '请输入模型型号' }]}
            extra="本地 TEI 忽略此值（部署时已定）；云端填真实型号"
          >
            <Input placeholder="如 Qwen3-Embedding-0.6B" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            rules={editing ? [] : [{ required: true, message: '请输入 api_key' }]}
            extra={editing ? '留空表示保留旧 key' : '本地 TEI 填任意非空值（如 dummy）'}
          >
            <Input.Password autoComplete="new-password" placeholder={editing ? '留空保留旧 key' : 'dummy'} />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            rules={[{ required: true, message: '请输入端点地址' }]}
            extra="OpenAI 兼容端点，含 /v1；compose 内 TEI 为 http://tei:8082/v1"
          >
            <Input placeholder="如 http://tei:8082/v1" />
          </Form.Item>
          <Flex gap={24}>
            <Form.Item
              name="dims"
              label="维度"
              tooltip="MRL 截断维度（如 Qwen3 原生 1024 → 512）；留空=模型原生。保存时经端点实测校验"
              rules={[{ type: 'number', min: 2, max: 4096, message: '维度范围 2–4096' }]}
            >
              <InputNumber min={2} max={4096} precision={0} style={{ width: 150 }} placeholder="原生" />
            </Form.Item>
            <Form.Item
              name="batch_size"
              label="批大小"
              tooltip="单请求文本数，须 ≤ TEI --max-client-batch-size"
              rules={[{ required: true }]}
            >
              <InputNumber min={1} max={128} precision={0} style={{ width: 150 }} />
            </Form.Item>
          </Flex>
          <Flex gap={24}>
            <Form.Item name="timeout" label="超时（秒）" rules={[{ required: true }]}>
              <InputNumber min={1} max={600} precision={0} style={{ width: 150 }} />
            </Form.Item>
            <Form.Item
              name="check_ctx_length"
              label="ctx 长度校验"
              valuePropName="checked"
              tooltip="TEI 必须关闭（tiktoken 规避）；真实 OpenAI 云端开启"
            >
              <Switch />
            </Form.Item>
          </Flex>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Drawer>
    </PageContainer>
  )
}
