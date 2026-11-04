/* 知识库管理页（plan §5.8）—— KB CRUD + 文档入口。
    - 知识库 = 一组文档的检索集合（es_index_name 不可改）。
    - 创建/编辑/删除/文档入库为管理员操作（后端 403 兜底）；非管理员只读预览。
    - 向量模型经下拉绑定（维度随行，plan kb-embedding-model-registry）：换绑 =
      破坏性变更（清索引块 + 文档重置待解析），有文档时二次确认。
    - 能力驱动（F2）：后端 kb_enabled=false 时路由未挂载（接口 404），本页依据
      全局能力开关（/health → capabilities）降级为禁用态提示。
    - 文档管理（上传/手动解析/检索）在抽屉 KbDocumentsDrawer 内完成。 */
import { App as AntApp, Button, Drawer, Flex, Form, Input, Popconfirm, Select, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useState, type ReactNode } from 'react'
import { FolderOpen, Pencil, Plus, Trash2 } from 'lucide-react'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useCreateKb, useDeleteKb, useKnowledgeBases, useUpdateKb } from '@/hooks/useKnowledge'
import { useEmbeddings } from '@/hooks/useEmbeddings'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { formatDateTime } from '@/lib/format'
import { KbDocumentsDrawer } from '@/features/knowledge/KbDocumentsDrawer'
import type { KnowledgeBaseOut, KnowledgeBaseUpdate, UUID } from '@/types'

interface FormValues {
  name: string
  description?: string
  embedding_id?: UUID | null
}

export function KnowledgePage(): ReactNode {
  const { ready, kb_enabled } = useCapabilities()
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)
  const { data, isLoading, isError, refetch } = useKnowledgeBases()
  const { data: embeddings } = useEmbeddings()
  const createKb = useCreateKb()
  const updateKb = useUpdateKb()
  const deleteKb = useDeleteKb()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<KnowledgeBaseOut | null>(null)
  const [docKb, setDocKb] = useState<KnowledgeBaseOut | null>(null)
  const [form] = Form.useForm<FormValues>()

  // 全局 KB 能力未启用（kb_enabled=false）→ 降级展示（不依赖接口 404/503）
  const kbDisabled = ready && !kb_enabled

  // 候选 = 已启用向量模型（含禁用的绑定行仅展示，不进新建候选）
  const embOptions = (embeddings ?? [])
    .filter((e) => e.enabled)
    .map((e) => ({
      value: e.id,
      label: `${e.name}（${e.dims ?? '原生'}维）`,
    }))

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ embedding_id: embOptions.length === 1 ? embOptions[0].value : null })
    setOpen(true)
  }

  const openEdit = (record: KnowledgeBaseOut) => {
    setEditing(record)
    form.setFieldsValue({
      name: record.name,
      description: record.description ?? '',
      embedding_id: record.embedding_id ?? null,
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
    const embeddingId = v.embedding_id ?? null
    if (editing) {
      const rebind = embeddingId !== (editing.embedding_id ?? null)
      const submit = (force: boolean) =>
        updateKb.mutateAsync({
          id: editing.id,
          body: {
            name: v.name.trim(),
            description: v.description?.trim() || null,
            ...(rebind ? { embedding_id: embeddingId, force } : {}),
          } satisfies KnowledgeBaseUpdate,
        })
      if (rebind && editing.doc_count > 0) {
        // 换绑守卫（前端预检；后端 409 needs_force 为兜底）：重置而非删除
        modal.confirm({
          title: `确认更换「${editing.name}」的向量模型？`,
          content:
            `将清空该库全部索引块，${editing.doc_count} 个文档重置为待解析状态` +
            '（原始文件保留、不删除），之后需逐个重新解析方可检索。',
          okType: 'danger',
          okText: '确认更换',
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
        embedding_id: embeddingId,
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
      width: 280,
      align: 'left',
      render: (v: string, r) => (
        <Flex vertical gap={2}>
          <span style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {v}
          </span>
          {r.description ? (
            <span
              style={{
                fontSize: 12,
                color: 'var(--color-text-tertiary)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {r.description}
            </span>
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
      title: '向量模型',
      dataIndex: 'embedding_name',
      key: 'embedding_name',
      width: 220,
      render: (v: string | null | undefined, r) =>
        v ? (
          <Flex gap={6} align="center">
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</span>
            {r.dims != null ? <Tag color="geekblue">{r.dims}维</Tag> : null}
          </Flex>
        ) : (
          <Tooltip title="未绑定（dims + 全局 env 双读回落，迁移期语义）">
            <Tag>全局默认{r.dims != null ? ` · ${r.dims}维` : ''}</Tag>
          </Tooltip>
        ),
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
        <Flex gap={4} justify="center">
          <Button
            type="link"
            size="small"
            icon={<FolderOpen size={14} />}
            onClick={() => setDocKb(record)}
          >
            文档
          </Button>
          {isManager ? (
            <>
              <Tooltip title="编辑">
                <Button type="link" size="small" icon={<Pencil size={14} />} onClick={() => openEdit(record)} />
              </Tooltip>
              <Popconfirm title="删除该知识库？" onConfirm={() => handleDelete(record)} okText="删除" cancelText="取消">
                <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
              </Popconfirm>
            </>
          ) : null}
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
        isManager ? (
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate} disabled={kbDisabled}>
            新建知识库
          </Button>
        ) : null
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

      {/* 新建/编辑抽屉：es_index_name 不可改；换绑向量模型为破坏性变更（有文档须确认重置） */}
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
            name="embedding_id"
            label="向量模型"
            tooltip="入库与检索的嵌入端点（维度随行）。更换已建库的向量模型为破坏性变更：索引块清空、文档重置为待解析"
            extra={
              embOptions.length
                ? undefined
                : '暂无已启用的向量模型，将使用全局默认端点（可在「向量模型」页注册）'
            }
          >
            <Select
              allowClear
              placeholder="全局默认端点"
              options={embOptions}
              // 编辑已绑定库：保持当前绑定可选中（即使该模型已被禁用）
              {...(editing?.embedding_id && !embOptions.some((o) => o.value === editing.embedding_id)
                ? {
                    options: [
                      ...embOptions,
                      {
                        value: editing.embedding_id,
                        label: `${editing.embedding_name ?? '已绑定'}（已禁用）· ${
                          editing.dims ?? '原生'
                        }维`,
                      },
                    ],
                  }
                : {})}
            />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 文档管理抽屉：上传/列表/检索/重建索引。key=kb.id：切换知识库时整体
          重挂载，检索调试的 query/results/viewer 状态随库切换清空，杜绝 A 库
          检索结果残留到 B 库 */}
      <KbDocumentsDrawer key={docKb?.id ?? 'none'} kb={docKb} open={!!docKb} onClose={() => setDocKb(null)} />
    </PageContainer>
  )
}
