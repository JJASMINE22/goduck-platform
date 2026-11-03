/* 技能管理页（DESIGN §6）—— 全局 skills 池共享，一个 skill = 一个子目录。
   上传支持目录结构：拖拽文件夹会递归展开，按相对路径（scripts/run.js）上传，
   后端在 <name>/ 下重建子目录并防目录穿越。
   界面只展示技能名；描述来自 SKILL.md frontmatter（GET /skills/{name}/description）。 */
import { App as AntApp, Button, Form, Input, Modal, Popconfirm, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { FolderOpen, Plus, Trash2, Upload as UploadIcon } from 'lucide-react'
import { useDeleteSkill, useSkills, useUploadSkill } from '@/hooks/useSkills'
import { useCurrentUser } from '@/store/userStore'
import { PageContainer } from '@/components/layout/PageContainer'
import { EmptyState, ErrorState, PageLoading } from '@/components/feedback/states'
import { SkillDescriptionText } from '@/components/skills/SkillDescriptionText'
import { toastError } from '@/lib/antdStatic'
import { fromDataTransfer, fromInputFiles, stripWrapperMatchingName, type FlatFileEntry } from '@/lib/flattenFiles'
import type { SkillInfo } from '@/types'

export function SkillsPage(): ReactNode {
  // 上传/删除为管理员专属；非管理员只读浏览技能池
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)
  const { data, isLoading, isError, refetch } = useSkills()
  const uploadSkill = useUploadSkill()
  const deleteSkill = useDeleteSkill()
  const { modal } = AntApp.useApp()

  const [open, setOpen] = useState(false)
  const [entries, setEntries] = useState<FlatFileEntry[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const dirInputRef = useRef<HTMLInputElement>(null)
  const [form] = Form.useForm<{ name: string }>()

  const openCreate = () => {
    form.resetFields()
    setEntries([])
    setOpen(true)
  }

  // 合并去重（按相对路径）
  const mergeEntries = (incoming: FlatFileEntry[]) => {
    setEntries((prev) => {
      const map = new Map(prev.map((e) => [e.path, e]))
      for (const e of incoming) map.set(e.path, e)
      return Array.from(map.values())
    })
  }

  const onDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    // 拖拽原样保留相对路径（含子目录），不做任何剥离，避免丢失目录层级
    const flat = await fromDataTransfer(e.dataTransfer)
    if (!flat.length) {
      toastError('未能读取文件（浏览器可能拒绝访问该文件夹）')
      return
    }
    mergeEntries(flat)
  }

  const onPickFiles = (e: ChangeEvent<HTMLInputElement>) => {
    // fromInputFiles 内部仅在「选择文件夹」时剥离所选文件夹名，其余层级原样保留
    mergeEntries(fromInputFiles(e.target.files))
    e.target.value = '' // 允许重复选择同名文件
  }

  // 「选择文件夹」专用：webkitdirectory 必须生效，否则 File 不带 webkitRelativePath，
  // 子目录层级会丢失。这里做守卫：若发现没有相对路径，明确报错并引导改用拖拽，
  // 避免静默地把 scripts/*.js 平铺到根目录。
  const onPickFolder = (e: ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files && files.length) {
      const hasRel = Array.from(files).some(
        (f) => !!(f as File & { webkitRelativePath?: string }).webkitRelativePath,
      )
      if (!hasRel) {
        toastError('当前浏览器未进入「目录选择」模式（webkitdirectory 未生效），子目录会丢失。请改为「拖拽整个文件夹」到上传区域。')
      }
    }
    mergeEntries(fromInputFiles(files))
    e.target.value = ''
  }

  // 「选择文件夹」input 的 ref 回调：元素挂载即命令式打上 webkitdirectory 等
  // 非标准属性（React 对其渲染不可靠），确保进入目录选择模式、File 带 webkitRelativePath。
  // 用 useCallback 保持稳定引用，避免每次渲染 detach/reattach。
  const setDirInput = useCallback((el: HTMLInputElement | null) => {
    dirInputRef.current = el
    if (el) {
      el.setAttribute('webkitdirectory', '')
      el.setAttribute('directory', '')
      el.setAttribute('mozdirectory', '')
    }
  }, [])

  // 兜底：弹窗打开后再确认一次属性已就位（应对 portal 挂载时序）。
  useEffect(() => {
    if (!open) return
    const el = dirInputRef.current
    if (el) {
      el.setAttribute('webkitdirectory', '')
      el.setAttribute('directory', '')
    }
  }, [open])

  const removeEntry = (path: string) => setEntries((prev) => prev.filter((e) => e.path !== path))

  const handleUpload = async () => {
    const v = await form.validateFields()
    if (!entries.length) {
      toastError('请选择至少一个文件')
      return
    }
    // 拖拽整个技能文件夹时，顶层目录名与技能名相同 → 剥掉它；其余层级（scripts/ 等）保留
    const finalEntries = stripWrapperMatchingName(entries, v.name.trim())
    await uploadSkill.mutateAsync({ name: v.name, files: finalEntries })
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
      width: 260,
      ellipsis: true,
      render: (v: string) => <code>{v}</code>,
    },
    {
      title: '描述',
      key: 'description',
      align: 'left',
      render: (_, r) => <SkillDescriptionText name={r.name} />,
    },
    {
      title: '文件数',
      dataIndex: 'file_count',
      key: 'file_count',
      width: 100,
      render: (v: number) => <Tag>{v}</Tag>,
    },
  ]
  // 操作列仅管理员渲染（普通用户整列隐藏，写操作本就后端 403 兜底）
  if (isManager) {
    columns.push({
      title: '操作',
      key: 'actions',
      width: 80,
      render: (_, r) => (
        <Popconfirm title="删除该技能？" onConfirm={() => handleDelete(r)} okText="删除" cancelText="取消">
          <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
        </Popconfirm>
      ),
    })
  }

  const selectedList = useMemo(() => [...entries].sort((a, b) => a.path.localeCompare(b.path)), [entries])

  return (
    <PageContainer
      title="技能列表"
      subtitle="个人技能池，跨智能体共享；描述取自技能内 SKILL.md"
      extra={
        isManager ? (
          <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>
            上传技能
          </Button>
        ) : null
      }
    >
      {isLoading ? (
        <PageLoading />
      ) : isError ? (
        <ErrorState onRetry={() => refetch()} />
      ) : !data?.length ? (
        <EmptyState title="暂无技能" extra={isManager ? <Button type="primary" icon={<Plus size={16} />} onClick={openCreate}>上传技能</Button> : null} />
      ) : (
        <Table<SkillInfo>
          rowKey="name"
          columns={columns}
          dataSource={data}
          pagination={false}
          size="middle"
          tableLayout="fixed"
          expandable={{
            rowExpandable: () => true,
            // 行展开查看描述全文（列内为单行省略）；镜像 MCP 页展开区样式
            expandedRowRender: (r) => (
              <div className="skill-desc-panel">
                <SkillDescriptionText name={r.name} full />
              </div>
            ),
          }}
        />
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
          <Form.Item label="文件（可多选，支持文件夹与目录结构）" required>
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setDragOver(true)
              }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              style={{
                border: `1px dashed ${dragOver ? 'var(--color-primary)' : 'var(--color-border)'}`,
                borderRadius: 8,
                padding: '20px 12px',
                textAlign: 'center',
                background: dragOver ? 'var(--color-bg-hover, rgba(0,0,0,0.02))' : 'transparent',
                transition: 'border-color .2s, background .2s',
                cursor: 'pointer',
              }}
              onClick={() => fileInputRef.current?.click()}
            >
              <p style={{ margin: 0, color: 'var(--color-text-secondary)' }}>
                <UploadIcon size={28} />
              </p>
              <p style={{ margin: '8px 0 4px' }}>拖拽文件夹到此区域（推荐，完整保留子目录层级）</p>
              <p style={{ margin: 0, fontSize: 12, color: 'var(--color-text-tertiary, var(--color-text-secondary))' }}>
                或点击本区域选择散文件；子目录结构原样保留（如 scripts/run.js）
              </p>
              {/* 选择文件夹需 webkitdirectory，单独一个按钮；点击时阻止冒泡以免触发整区选择文件 */}
              <div style={{ marginTop: 12 }} onClick={(e) => e.stopPropagation()}>
                <Button size="small" icon={<FolderOpen size={14} />} onClick={() => dirInputRef.current?.click()}>
                  选择文件夹
                </Button>
              </div>
            </div>

            {selectedList.length > 0 && (
              <ul style={{ margin: '12px 0 0', padding: 0, listStyle: 'none', maxHeight: 200, overflow: 'auto' }}>
                {selectedList.map((e) => (
                  <li
                    key={e.path}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      gap: 8,
                      padding: '4px 0',
                      borderBottom: '1px solid var(--color-border)',
                    }}
                  >
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      <code>{e.path}</code>
                    </span>
                    <Button
                      type="link"
                      size="small"
                      danger
                      icon={<Trash2 size={13} />}
                      onClick={() => removeEntry(e.path)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </Form.Item>
        </Form>

        {/* 触发用原生 input：移出 Form.Item 并 display:none，避免被 antd 当作表单控件渲染出“选择文件”按钮。
            - multiple：点击上传区 → 普通多选文件
            - 选择文件夹：webkitdirectory（上面 useEffect 命令式打属性）→ 整目录，
              浏览器展开并给每个 File 填充 webkitRelativePath（含子目录层级） */}
        <input ref={fileInputRef} type="file" multiple style={{ display: 'none' }} onChange={onPickFiles} />
        <input ref={setDirInput} type="file" style={{ display: 'none' }} onChange={onPickFolder} />
      </Modal>
    </PageContainer>
  )
}
