/* 知识库管理页（plan §5.8）—— KB CRUD + 文档入口。
   - 知识库 = 一组文档的检索集合（es_index_name 不可改）。
   - 创建需 creator（取顶栏当前用户）；可设嵌入维度（默认全局）；改名/改维度时后端
     校验唯一性（409）与维度守卫（有文档须 force：文档重置为待解析，不删除）。
   - 能力驱动（F2）：后端 kb_enabled=false 时路由未挂载（接口 404），本页依据
     全局能力开关（/health → capabilities）降级为禁用态提示，不依赖接口报错。
   - 文档管理（上传/手动解析/检索）在抽屉 KbDocumentsDrawer 内完成。 */
import { App as AntApp, Button, Drawer, Flex, Form, Input, InputNumber, Popconfirm, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { FolderOpen, Pencil, Plus, Trash2 } from 'lucide-react'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useCreateKb, useDeleteKb, useKnowledgeBases, useUpdateKb } from '@/hooks/useKnowledge'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatDateTime } from '@/lib/format'
import { KbDocumentsDrawer } from '@/features/knowledge/KbDocumentsDrawer'
import type { KnowledgeBaseOut, KnowledgeBaseUpdate } from '@/types'

interface FormValues {
  name: string
  description?: string
  dims?: number | null
}

export function KnowledgePage(): ReactNode {
  const { ready, kb_enabled } = useCapabilities()
  const { data, isLoading, isError, refetch } = useKnowledgeBases()
  const createKb = useCreateKb()
  const updateKb = useUpdateKb()
  const deleteKb = useDeleteKb()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<KnowledgeBaseOut | null>(null)
  const [docKb, setDocKb] = useState<KnowledgeBaseOut | null>(null)
  const [form] = Form.useForm<FormValues>()

  const userId = useCurrentUser((s) => s.userId)

  // 全局 KB 能力未启用（kb_enabled=false）→ 降级展示（不依赖接口 404/503）
  const kbDisabled = ready && !kb_enabled

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    setOpen(true)
  }

  const openEdit = (record: KnowledgeBaseOut) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name,
      description: record.description ?? '',
      dims: record.dims ?? undefined,
    })
    setOpen(true)
  }

  const handleSave = async () => {
    let v: FormValues
    try {
      v = await form.validateFields()
    } catch {
      return // 校验失败：表单内联提示
    }
    const dims = v.dims ?? null
    if (editing) {
      const dimsChanged = (dims ?? null) !== (editing.dims ?? null)
      const submit = (force: boolean) =>
        updateKb.mutateAsync({
          id: editing.id,
          body: {
            name: v.name.trim(),
            description: v.description?.trim() || null,
            ...(dimsChanged ? { dims, force } : {}),
          } satisfies KnowledgeBaseUpdate,
        })
      if (dimsChanged && editing.doc_count > 0) {
        // 维度守卫（前端预检；后端 409 needs_force 为兜底）：重置而非删除
        modal.confirm({
          title: `确认修改「${editing.name}」的维度？`,
          content:
            `将清空该库全部索引块，${editing.doc_count} 个文档重置为待解析状态` +
            '（原始文件保留、不删除），之后需逐个重新解析方可检索。',
          okType: 'danger',
          okText: '确认修改',
          cancelText: '取消',
          onOk: async () => {
            await submit(true)
            setOpen(false)
          },
        })
        return
      }
      await submit(false)
      setOpen(false)
    } else {
      await createKb.mutateAsync({
        name: v.name.trim(),
        description: v.description?.trim() || null,
        creator: userId,
        ...(dims != null ? { dims } : {}),
      })
      setOpen(false)
    }
  }

  const handleDelete = (record: KnowledgeBaseOut) => {
    modal.confirm({
      title: `删除知识库「${record.name}」？`,
      content: '将级联删除该库全部文档、索引块与 MinIO 留底，不可恢复。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: () => deleteKb.mutateAsync(record.id),
    })
  }

  const columns: ColumnsType<KnowledgeBaseOut> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (v: string, r) => (
        <Flex vertical gap={2}>
          <span style={{ fontWeight: 600 }}>{v}</span>
          {r.description ? (
            <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>{r.description}</span>
          ) : null}
        </Flex>
      ),
    },
    {
      title: '文档',
      dataIndex: 'doc_count',
      key: 'doc_count',
      width: 80,
      render: (v: number) => <Tag>{v}</Tag>,
    },
    {
      title: '索引块',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 90,
      render: (v: number) => <Tag color="blue">{v}</Tag>,
    },
    {
      title: '维度',
      dataIndex: 'dims',
      key: 'dims',
      width: 80,
      render: (v?: number | null) => v ?? '—',
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
      width: 150,
      render: (_, record) => (
        <Flex gap={4}>
          <Button
            type="link"
            size="small"
            icon={<FolderOpen size={14} />}
            onClick={() => setDocKb(record)}
          >
            文档
          </Button>
          <Tooltip title="编辑">
            <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(record)} />
          </Tooltip>
          <Popconfirm title="删除该知识库？" onConfirm={() => handleDelete(record)} okText="删除" cancelText="取消">
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    },
  ]

  const busy = createKb.isPending || updateKb.isPending

  return (
    <PageContainer
      title="知识库列表"
      subtitle="上传 pdf/docx 留底，手动解析入库；智能体经 kb_search 检索召回"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} disabled={kbDisabled}>
          新建知识库
        </Button>
      }
    >
      {isLoading ? (
        <PageLoading />
      ) : kbDisabled ? (
        <ErrorState
          message="知识库能力未启用（后端 kb_enabled=false）。请在后端配置并重启服务后重试。"
          onRetry={() => refetch()}
        />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState
          title="暂无知识库"
          description="新建一个知识库后即可上传文档"
          extra={
            <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
              新建知识库
            </Button>
          }
        />
      ) : (
        <Table<KnowledgeBaseOut>
          rowKey="id"
          columns={columns}
          dataSource={data}
          pagination={false}
          size="middle"
        />
      )}

      {/* 新建/编辑抽屉：es_index_name 不可改；维度创建后可改（有文档须确认重置） */}
      <Drawer
        title={editing ? `编辑知识库：${editing.name}` : '新建知识库'}
        open={open}
        onClose={() => setOpen(false)}
        width={460}
        destroyOnClose
        footer={
          <Flex justify="flex-end" gap={8}>
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button type="primary" loading={busy} onClick={() => void handleSave()}>
              保存
            </Button>
          </Flex>
        }
      >
        <Form<FormValues> form={form} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            rules={[
              { required: true, message: '请输入知识库名称' },
              { max: 64, message: '名称过长（≤64）' },
            ]}
          >
            <Input placeholder="如 产品手册" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} placeholder="选填，说明该库的用途与范围" />
          </Form.Item>
          <Form.Item
            name="dims"
            label="向量维度"
            tooltip="嵌入向量维度（须与嵌入服务能力匹配，如 MRL 截断 512/1024）。库内已有文档时修改需二次确认（文档将重置为待解析）"
            rules={[{ type: 'number', min: 2, max: 4096, message: '维度范围 2–4096' }]}
          >
            <InputNumber
              min={2}
              max={4096}
              precision={0}
              style={{ width: '100%' }}
              placeholder="默认使用全局维度"
            />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 文档管理抽屉：上传/列表/检索/重建索引 */}
      <KbDocumentsDrawer kb={docKb} open={!!docKb} onClose={() => setDocKb(null)} />
    </PageContainer>
  )
}
