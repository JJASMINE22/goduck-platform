/* 知识库文档管理抽屉 —— 选中某库后管理其文档：
   - 上传（拖拽/点击，仅 pdf/docx，服务端魔数判定）
   - 文档列表（状态卡 + 入库轮询 + 失败原因 tooltip）
   - 重建索引（需 MinIO 留底，否则后端 400）
   - 检索调试（管理面用，生产检索走 mcp-servers 的 kb_search） */
import {
  App as AntApp,
  Button,
  Drawer,
  Flex,
  InputNumber,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { FileText, RotateCw, Search, Trash2, Upload as UploadIcon } from 'lucide-react'
import { ApiError } from '@/api/client'
import {
  useDeleteDocument,
  useKbDocuments,
  useKbSearch,
  useReindexDocument,
  useUploadDocument,
} from '@/hooks/useKnowledge'
import { formatBytes, formatDateTime } from '@/lib/format'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import { KbDocStatusTag } from './KbDocStatusTag'
import type { KbDocumentOut, KbSearchResult, KnowledgeBaseOut } from '@/types'

const ACCEPT = '.pdf,.docx'
const ACCEPT_RE = /\.(pdf|docx)$/i

/** KB 仅收 pdf/docx —— 前端预筛减少无效上传（准则 F6：服务端魔数仍为最终判定） */
function isAcceptable(file: File): boolean {
  return ACCEPT_RE.test(file.name)
}

function scorePct(score: number): string {
  // ES kNN 余弦相似度通常落在 -1..1，转百分比仅供直观，非精确语义
  return `${Math.max(0, Math.min(100, Math.round(score * 100)))}%`
}

export function KbDocumentsDrawer({
  kb,
  open,
  onClose,
}: {
  kb: KnowledgeBaseOut | null
  open: boolean
  onClose: () => void
}): ReactNode {
  const kbId = kb?.id ?? ''
  const { data, isLoading, isError, refetch } = useKbDocuments(open ? kbId : null)
  const uploadDoc = useUploadDocument(kbId)
  const deleteDoc = useDeleteDocument(kbId)
  const reindexDoc = useReindexDocument(kbId)
  const search = useKbSearch(kbId)
  const { modal } = AntApp.useApp()

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(5)
  const [results, setResults] = useState<KbSearchResult[]>([])

  /** 顺序上传多文件：并发上传会挤占入库池（kb_ingestion_concurrency），且失败定位困难 */
  const uploadFiles = async (files: FileList | File[]) => {
    const list = Array.from(files)
    const ok = list.filter(isAcceptable)
    const rejected = list.length - ok.length
    if (rejected > 0) toastError(`已跳过 ${rejected} 个非 pdf/docx 文件`)
    if (!ok.length) return
    for (const f of ok) {
      try {
        await uploadDoc.mutateAsync(f)
      } catch {
        // 单文件失败不阻断后续；错误由全局 mutation onError 弹出
      }
    }
  }

  const onPickFiles = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) void uploadFiles(e.target.files)
    e.target.value = '' // 允许重复选择同名文件
  }

  const onDrop = async (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    if (e.dataTransfer.files?.length) void uploadFiles(e.dataTransfer.files)
  }

  const handleDelete = (doc: KbDocumentOut) => {
    modal.confirm({
      title: `删除文档「${doc.filename}」？`,
      content: '将移除该文档的全部索引块与原始留底，不可恢复。',
      okType: 'danger',
      okText: '删除',
      cancelText: '取消',
      onOk: () => deleteDoc.mutateAsync(doc.id),
    })
  }

  const handleReindex = async (doc: KbDocumentOut) => {
    try {
      await reindexDoc.mutateAsync(doc.id)
      toastSuccess('已重新投递入库')
    } catch (e) {
      // 未保留原始字节等 400：detail 已由全局弹窗展示，这里兜底静默
      if (!(e instanceof ApiError)) toastError('重建索引失败，请重试')
    }
  }

  const handleSearch = async () => {
    const q = query.trim()
    if (!q) {
      toastError('请输入检索内容')
      return
    }
    try {
      const res = await search.mutateAsync({ q, k: topK })
      setResults(res)
      if (!res.length) toastSuccess('无匹配结果')
    } catch {
      // 维度漂移等错误由全局弹窗展示
    }
  }

  const columns: ColumnsType<KbDocumentOut> = [
    {
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      render: (v: string, r) => (
        <Tooltip title={v}>
          <Space size={6}>
            <FileText size={14} style={{ flexShrink: 0, color: 'var(--color-text-tertiary)' }} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {v}
            </span>
            <Tag style={{ marginInlineStart: 0 }}>{r.mime.split('/').pop()?.toUpperCase()}</Tag>
          </Space>
        </Tooltip>
      ),
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 90,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '页/段',
      dataIndex: 'page_count',
      key: 'page_count',
      width: 80,
      render: (v?: number | null) => (v ?? '—'),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 100,
      render: (v: string, r) =>
        v === 'failed' && r.error ? (
          <Tooltip title={r.error}>
            <span>
              <KbDocStatusTag status={v} />
            </span>
          </Tooltip>
        ) : (
          <KbDocStatusTag status={v} />
        ),
    },
    {
      title: '块数',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 70,
      render: (v?: number | null) => v ?? '—',
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 160,
      render: (v: string) => formatDateTime(v),
    },
    {
      title: '操作',
      key: 'actions',
      width: 96,
      render: (_, r) => (
        <Flex gap={4}>
          <Tooltip title="重建索引（需 MinIO 留底）">
            <Button
              type="link"
              size="small"
              icon={<RotateCw size={14} />}
              loading={reindexDoc.isPending}
              onClick={() => handleReindex(r)}
            />
          </Tooltip>
          <Popconfirm
            title="删除该文档？"
            onConfirm={() => handleDelete(r)}
            okText="删除"
            cancelText="取消"
          >
            <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
          </Popconfirm>
        </Flex>
      ),
    },
  ]

  return (
    <Drawer
      title={kb ? `文档管理：${kb.name}` : '文档管理'}
      open={open}
      onClose={onClose}
      width={960}
      destroyOnClose
      styles={{ body: { padding: 16 } }}
    >
      {kb ? (
        <Flex vertical gap={16}>
          <Typography.Text type="secondary">
            文档 {kb.doc_count} · 索引块 {kb.chunk_count}
            {kb.description ? ` · ${kb.description}` : ''}
          </Typography.Text>

          {/* 上传区 */}
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
              padding: '18px 12px',
              textAlign: 'center',
              background: dragOver ? 'var(--color-bg-hover, rgba(0,0,0,0.02))' : 'transparent',
              transition: 'border-color .2s, background .2s',
              cursor: 'pointer',
            }}
            onClick={() => fileInputRef.current?.click()}
          >
            <p style={{ margin: 0, color: 'var(--color-text-secondary)' }}>
              <UploadIcon size={24} />
            </p>
            <p style={{ margin: '6px 0 2px' }}>拖拽 PDF / DOCX 到此，或点击选择</p>
            <p style={{ margin: 0, fontSize: 12, color: 'var(--color-text-tertiary)' }}>
              支持多文件；仅 pdf/docx，入库在后台异步进行
            </p>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT}
              multiple
              style={{ display: 'none' }}
              onChange={onPickFiles}
            />
          </div>

          {/* 文档表 */}
          <Table<KbDocumentOut>
            rowKey="id"
            size="small"
            loading={isLoading}
            columns={columns}
            dataSource={data}
            pagination={false}
            locale={{
              emptyText: isError ? '加载失败' : '暂无文档，上传第一个吧',
            }}
          />

          {/* 检索调试 */}
          <div
            style={{
              marginTop: 8,
              borderTop: '1px solid var(--color-border)',
              paddingTop: 12,
            }}
          >
            <Typography.Text strong>检索调试</Typography.Text>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 4, marginBottom: 8 }}>
              验证入库内容能否被召回；生产检索由智能体经 kb_search 工具发起。
            </Typography.Paragraph>
            <Flex gap={8} wrap>
              <input
                className="kb-search-input"
                placeholder="输入检索内容"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void handleSearch()
                }}
                style={{
                  flex: 1,
                  minWidth: 200,
                  height: 32,
                  padding: '4px 11px',
                  borderRadius: 6,
                  border: '1px solid var(--color-border)',
                }}
              />
              <InputNumber
                min={1}
                max={50}
                value={topK}
                onChange={(v) => setTopK(typeof v === 'number' ? v : 5)}
                style={{ width: 90 }}
                addonBefore="TopK"
              />
              <Button
                type="primary"
                icon={<Search size={14} />}
                loading={search.isPending}
                onClick={() => void handleSearch()}
              >
                检索
              </Button>
              <Button
                disabled={!results.length && !isError}
                onClick={() => refetch()}
              >
                刷新
              </Button>
            </Flex>

            {results.length > 0 ? (
              <Flex vertical gap={8} style={{ marginTop: 12 }}>
                {results.map((r, i) => (
                  <SearchHit key={`${r.doc_id}-${r.chunk_index ?? i}`} hit={r} />
                ))}
              </Flex>
            ) : null}
          </div>
        </Flex>
      ) : null}
    </Drawer>
  )
}

/** 单条检索命中：来源 + 分页定位 + 内容片段 + 相似度 */
function SearchHit({ hit }: { hit: KbSearchResult }): ReactNode {
  return (
    <div
      style={{
        border: '1px solid var(--color-border)',
        borderRadius: 8,
        padding: '8px 12px',
        background: 'var(--color-bg-hover, rgba(0,0,0,0.02))',
      }}
    >
      <Flex justify="space-between" align="center" gap={8}>
        <Space size={6} wrap>
          <FileText size={13} style={{ color: 'var(--color-text-tertiary)' }} />
          <Typography.Text ellipsis style={{ maxWidth: 360, fontWeight: 500 }}>
            {hit.filename}
          </Typography.Text>
          {hit.page ? <Tag>P{hit.page}</Tag> : null}
          {hit.chunk_index != null ? <Tag>#{hit.chunk_index}</Tag> : null}
        </Space>
        <Tag color="blue">{scorePct(hit.score)}</Tag>
      </Flex>
      <Typography.Paragraph
        ellipsis={{ rows: 3 }}
        style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--color-text-secondary)' }}
      >
        {hit.content}
      </Typography.Paragraph>
    </div>
  )
}
