/* 技能管理页（DESIGN §6）—— 全局 skills 池共享，一个 skill = 一个子目录。
   multipart 上传（name + 多文件，防目录穿越）。AgentConfig.skills 引用为 /skills/<name>/。 */
import { App as AntApp, Button, Form, Input, Modal, Popconfirm, Table, Tag, Upload } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { UploadFile } from 'antd/es/upload/interface'
import { useState, type ReactNode } from 'react'
import { Plus, Trash2, Upload as UploadIcon } from 'lucide-react'
import { useDeleteSkill, useSkills, useUploadSkill } from '@/hooks/useSkills'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { toastError } from '@/lib/antdStatic'
import type { SkillInfo } from '@/types'

export function SkillsPage(): ReactNode {
  const { data, isLoading, isError, refetch } = useSkills()
  const uploadSkill = useUploadSkill()
  const deleteSkill = useDeleteSkill()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [fileList, setFileList] = useState<UploadFile[]>([])
  const [form] = Form.useForm<{ name: string }>()

  const openCreate = () => {
    form.resetFields()
    setFileList([])
    setOpen(true)
  }

  const handleUpload = async () => {
    const v = await form.validateFields()
    const files = fileList.map((f) => f.originFileObj).filter(Boolean) as unknown as File[]
    if (!files.length) {
      toastError('请选择至少一个文件')
      return
    }
    await uploadSkill.mutateAsync({ name: v.name, files })
    setOpen(false)
  }

  const handleDelete = (record: SkillInfo) => {
    modal.confirm({
      title: `删除技能「${record.name}」？`,
      content: '该技能目录将被整体删除，引用它的智能体配置不受影响（但运行时将找不到该技能）。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: () => deleteSkill.mutateAsync(record.name),
    })
  }

  const columns: ColumnsType<SkillInfo> = [
    {
      title: '技能名',
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <code>/skills/{v}/</code>,
    },
    {
      title: '文件数',
      dataIndex: 'file_count',
      key: 'file_count',
      width: 100,
      render: (v: number) => <Tag>{v}</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 80,
      render: (_, r) => (
        <Popconfirm title="删除该技能？" onConfirm={() => handleDelete(r)} okText="删除" cancelText="取消">
          <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
        </Popconfirm>
      ),
    },
  ]

  return (
    <PageContainer
      title="技能管理"
      subtitle="个人技能池，跨智能体共享；AgentConfig.skills 引用 /skills/<name>/"
      extra={
        <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
          上传技能
        </Button>
      }
    >
      {isLoading ? (
        <PageLoading />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState title="暂无技能" extra={<Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>上传技能</Button>} />
      ) : (
        <Table<SkillInfo> rowKey="name" columns={columns} dataSource={data} pagination={false} size="middle" />
      )}

      <Modal
        title="上传技能"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleUpload}
        okText="上传"
        cancelText="取消"
        confirmLoading={uploadSkill.isPending}
        destroyOnClose
      >
        <Form<{ name: string }> form={form} layout="vertical">
          <Form.Item
            name="name"
            label="技能名"
            rules={[
              { required: true, message: '请输入技能名' },
              { pattern: /^[A-Za-z0-9_-]+$/, message: '仅允许字母/数字/下划线/连字符' },
            ]}
          >
            <Input placeholder="如 pdf-extractor" />
          </Form.Item>
          <Form.Item label="文件（可多选，markdown/文本）" required>
            <Upload.Dragger
              multiple
              beforeUpload={() => false /* 阻止自动上传，由提交按钮统一处理 */}
              fileList={fileList}
              onChange={({ fileList: fl }) => setFileList(fl)}
            >
              <p style={{ margin: 0, color: 'var(--color-text-secondary)' }}>
                <UploadIcon size={28} />
              </p>
              <p style={{ margin: '8px 0 0' }}>点击或拖拽文件到此区域</p>
            </Upload.Dragger>
          </Form.Item>
        </Form>
      </Modal>
    </PageContainer>
  )
}
