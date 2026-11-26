/* 模型管理页（DESIGN §10.6）—— 注册表的增删改查 + 保存前 /test 连通探测。
   api_key 列表永不返回；编辑时留空 = 保留旧 key（轮换密钥）。
   端点（base_url）列仅管理员可见：普通用户只读预览不暴露端点地址。
   删除被引用模型 → 后端 409 含 referencing_agents，弹窗提示先解绑。
   编辑面板对齐标准注册页排版：身份/凭据字段垂直单列、说明收敛进
   placeholder 与 label tooltip；api_key 用 SecretInput（* 紧凑掩码）；
   参数区为行式配置面板（paramRow：label 定宽列 + 控件紧随，现代 LLM
   配置风格），思考模式三态用 Radio 按钮组。 */
import { App as AntApp, Button, Divider, Drawer, Flex, Form, Input, InputNumber, Popconfirm, Radio, Switch, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { Pencil, Plug, Plus, Trash2 } from 'lucide-react'
import {
  useCreateModel,
  useDeleteModel,
  useModels,
  useTestModel,
  useUpdateModel,
} from '@/hooks/useModels'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { SecretInput } from '@/components/form/SecretInput'
import { ApiError } from '@/api/client'
import { formatDateTime } from '@/lib/format'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import { useCurrentUser } from '@/store/userStore'
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
  enable_thinking: boolean | null
  enabled: boolean
  is_default_vision?: boolean
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
  enable_thinking: null,
  enabled: true,
  is_default_vision: false,
}

/* 参数区行式布局（现代 LLM 配置面板风格）：label 定宽列、控件紧随其后
   而非两端拉开，行距 12px；覆盖在垂直表单内（antd Form.Item layout） */
const paramRow = {
  layout: 'horizontal',
  labelCol: { flex: '112px' },
  wrapperCol: { flex: 'auto' },
  style: { marginBottom: 12 },
} as const

const numWidth = { width: 132 }

export function ModelsPage(): ReactNode {
  // 写操作（注册/编辑/删除/连通测试）为管理员专属；非管理员只读预览
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)
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
      is_default_vision: record.is_default_vision ?? false,
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
      width: 110,
      align: 'center',
      render: (v: boolean, record) =>
        v ? (
          <Flex gap={4} justify="center" wrap>
            <Tag color="blue">支持</Tag>
            {record.is_default_vision ? <Tag color="gold">默认</Tag> : null}
          </Flex>
        ) : (
          <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>
        ),
    },
    {
      title: '思考',
      dataIndex: 'enable_thinking',
      key: 'enable_thinking',
      width: 100,
      align: 'center',
      render: (v: boolean | null) =>
        v === true ? (
          <Tag color="purple">开</Tag>
        ) : v === false ? (
          <Tag color="purple">关</Tag>
        ) : (
          <span style={{ color: 'var(--color-text-tertiary)' }}>默认</span>
        ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 90,
      align: 'center',
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag>禁用</Tag>),
    },
    // 端点地址为管理员专属信息（普通用户只读预览不含此列，与"操作"列同款门控）
    ...(isManager
      ? [
          {
            title: '端点',
            dataIndex: 'base_url',
            key: 'base_url',
            align: 'left' as const,
            ellipsis: true,
            render: (v: string | null) =>
              v ? (
                <code style={{ fontSize: 12 }}>{v}</code>
              ) : (
                <span style={{ color: 'var(--color-text-tertiary)' }}>原生 OpenAI</span>
              ),
          },
        ]
      : []),
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 170,
      render: (v: string) => formatDateTime(v),
    },
  ]
  // 操作列仅管理员渲染（普通用户整列隐藏，写操作本就后端 403 兜底）
  if (isManager) {
    columns.push({
      title: '操作',
      key: 'actions',
      width: 120,
      render: (_, record) => (
        <Flex gap={4} justify="center">
          <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(record)} />
          <Popconfirm title="删除该模型？" onConfirm={() => handleDelete(record)} okText="删除" cancelText="取消">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    })
  }

  const busy = createModel.isPending || updateModel.isPending || testModel.isPending

  return (
    <PageContainer
      title="模型列表"
      subtitle="注册 OpenAI 兼容模型；智能体按 model_id 绑定"
      extra={
        isManager ? (
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
            注册模型
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
            title="暂无模型"
            description="注册一个 OpenAI 兼容模型后即可创建智能体"
            extra={
              isManager ? (
                <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
                  注册模型
                </Button>
              ) : null
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
          <Tooltip title="测试连通性，不落库">
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
        <Form<FormValues> form={form} layout="vertical" requiredMark={false} labelAlign="left" colon={false} initialValues={emptyForm}>
          {/* 客户端族当前唯一取值：收为隐藏字段，保留提交与 /test 语义 */}
          <Form.Item name="provider_kind" hidden>
            <Input />
          </Form.Item>
          <Form.Item name="name" label="展示名" rules={[{ required: true, message: '请输入展示名' }]}>
            <Input placeholder="如 我的OpenAI" allowClear />
          </Form.Item>
          <Form.Item name="model_type" label="模型型号" rules={[{ required: true, message: '请输入模型型号' }]}>
            <Input placeholder="如 gpt-4o-mini" allowClear />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            rules={editing ? [] : [{ required: true, message: '请输入 API Key' }]}
          >
            <SecretInput autoComplete="new-password" placeholder={editing ? '留空保留原 Key' : 'sk-...'} />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL">
            <Input placeholder="如 https://api.deepseek.com/v1，原生 OpenAI 留空" allowClear />
          </Form.Item>
          <Divider
            plain
            orientation="left"
            orientationMargin={0}
            style={{ margin: '4px 0 16px', color: 'var(--color-text-tertiary)', fontSize: 12 }}
          >
            参数
          </Divider>
          <Form.Item {...paramRow} name="temperature" label="温度">
            <InputNumber min={0} max={2} step={0.1} style={numWidth} placeholder="默认" />
          </Form.Item>
          <Form.Item {...paramRow} name="top_p" label="Top P">
            <InputNumber min={0} max={1} step={0.05} style={numWidth} placeholder="默认" />
          </Form.Item>
          <Form.Item
            {...paramRow}
            name="enable_thinking"
            label="思考模式"
            tooltip="跟随服务端默认；GLM V 系列等服务端默认开启思考，需关闭请选「关闭」"
          >
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              options={[
                { value: null, label: '默认' },
                { value: true, label: '开启' },
                { value: false, label: '关闭' },
              ]}
            />
          </Form.Item>
          <Form.Item {...paramRow} name="supports_vision" label="视觉支持" valuePropName="checked">
            <Switch />
          </Form.Item>
          {/* 平台默认视觉模型（kb/vision 解析链第二级）：仅视觉模型可设；全平台
              至多一个（置 true 后端自动清除其余行） */}
          <Form.Item noStyle shouldUpdate={(prev, cur) => prev.supports_vision !== cur.supports_vision}>
            {({ getFieldValue }) =>
              getFieldValue('supports_vision') ? (
                <Form.Item
                  {...paramRow}
                  name="is_default_vision"
                  label="默认视觉"
                  valuePropName="checked"
                  tooltip="智能体无下挂视觉模型时，图像理解工具回落到本模型（全平台至多一个）"
                >
                  <Switch />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          <Form.Item {...paramRow} name="enabled" label="启用" valuePropName="checked" style={{ marginBottom: 0 }}>
            <Switch />
          </Form.Item>
        </Form>
      </Drawer>
    </PageContainer>
  )
}
