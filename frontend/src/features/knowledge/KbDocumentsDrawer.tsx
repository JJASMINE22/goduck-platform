/* 知识库文档管理抽屉 —— 选中某库后管理其文档：
   - 上传（拖拽/点击，仅 pdf/docx，服务端魔数判定）—— 仅注册，不解析
   - 待解析文件面板（上传后出现）：统一参数栏（可一键应用到全部）+ 每文件独立
     chunk size / overlap 配置与单独解析 + 批量「全部解析」
   - 文档表（解析中/已解析/失败）：状态卡 + 轮询 + 失败原因 tooltip + 重新解析
   - 检索调试（管理面用，生产检索走 mcp-servers 的 kb_search） */
import {
  App as AntApp,
  Button,
  Drawer,
  Flex,
  InputNumber,
  Popconfirm,
  Popover,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useRef, useState, type ChangeEvent, type ReactNode } from 'react'
import { FileText, Play, Search, Trash2, Upload as UploadIcon } from 'lucide-react'
import { ApiError } from '@/api/client'
import {
  useDeleteDocument,
  useKbDocuments,
  useKbSearch,
  useParseDocument,
  useUploadDocument,
} from '@/hooks/useKnowledge'
import { useDragResize } from '@/hooks/useDragResize'
import { formatBytes, formatDateTime } from '@/lib/format'
import { toastError, toastSuccess } from '@/lib/antdStatic'
import { KbDocStatusTag } from './KbDocStatusTag'
import type { KbDocumentOut, KbSearchResult, KnowledgeBaseOut, UUID } from '@/types'

const ACCEPT = '.pdf,.docx'
const ACCEPT_RE = /\.(pdf|docx)$/i
/** 全局默认切块参数（与后端 kb_chunk_size/kb_chunk_overlap 一致，仅作占位提示） */
const DEFAULT_CHUNK_SIZE = 800
const DEFAULT_CHUNK_OVERLAP = 120

/** KB 仅收 pdf/docx —— 前端预筛减少无效上传（准则 F6：服务端魔数仍为最终判定） */
function isAcceptable(file: File): boolean {
  return ACCEPT_RE.test(file.name)
}

function fileExtTag(filename: string): string {
  const m = /\.(\w+)$/.exec(filename)
  return (m?.[1] ?? 'FILE').toUpperCase()
}

function scorePct(score: number): string {
  // ES kNN 余弦相似度通常落在 -1..1，转百分比仅供直观，非精确语义
  return `${Math.max(0, Math.min(100, Math.round(score * 100)))}%`
}

/** 每文件参数覆盖值（panel 内 state）：键存在=用户已干预；null=清空回默认 */
interface ChunkParams {
  size: number | null
  overlap: number | null
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
  const parseDoc = useParseDocument(kbId)
  const search = useKbSearch(kbId)
  const { modal } = AntApp.useApp()

  // 抽屉可拖拽调宽（左边缘手柄，默认隐形、悬停浮现）；宽度持久化到 localStorage。
  const { size: drawerWidth, onResizeStart, reset } = useDragResize({
    initial: 960,
    min: 560,
    max: Math.min(1800, window.innerWidth - 24),
    storageKey: 'kb-doc-drawer-width',
  })

  const fileInputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(5)
  const [results, setResults] = useState<KbSearchResult[]>([])

  // 待解析（pending）进配置面板；其余（indexing/indexed/failed）进文档表
  const pendingDocs = (data ?? []).filter((d) => d.status === 'pending')
  const parsedDocs = (data ?? []).filter((d) => d.status !== 'pending')

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

  /** 单文档解析（面板单行 / 表内重新解析共用；null=沿用已存值/全局默认）。
   *  返回是否投递成功（批量计数用；错误提示已在此处理）。 */
  const handleParse = async (
    doc: KbDocumentOut,
    chunkSize?: number | null,
    chunkOverlap?: number | null
  ): Promise<boolean> => {
    try {
      await parseDoc.mutateAsync({
        docId: doc.id,
        body: { chunk_size: chunkSize ?? null, chunk_overlap: chunkOverlap ?? null },
      })
      toastSuccess(doc.status === 'pending' ? '已开始解析' : '已重新投递解析')
      return true
    } catch (e) {
      // 解析中重复投递（409）/ 无留底（400）等：detail 已由全局弹窗展示
      if (!(e instanceof ApiError)) toastError('解析失败，请重试')
      return false
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
      render: (v: string) => (
        <Tooltip title={v}>
          <Space size={6}>
            <FileText size={14} style={{ flexShrink: 0, color: 'var(--color-text-tertiary)' }} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {v}
            </span>
            <Tag style={{ marginInlineStart: 0 }}>{fileExtTag(v)}</Tag>
          </Space>
        </Tooltip>
      ),
    },
    {
      title: '切块参数',
      key: 'chunk_params',
      width: 130,
      render: (_, r) =>
        r.chunk_size != null ? (
          <Tooltip title="chunk size / overlap（解析生效值，重建沿用）">
            <Tag style={{ marginInlineEnd: 0 }}>
              {r.chunk_size} / {r.chunk_overlap ?? 0}
            </Tag>
          </Tooltip>
        ) : (
          <span style={{ color: 'var(--color-text-tertiary)' }}>—</span>
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
      width: 140,
      render: (v: string, r) =>
        v === 'failed' && r.error ? (
          <Tooltip title={r.error}>
            <span>
              <KbDocStatusTag status={v} />
            </span>
          </Tooltip>
        ) : (
          <KbDocStatusTag status={v} progress={r.progress} />
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
          <ParsePopover
            loading={parseDoc.isPending}
            disabled={r.status === 'indexing'}
            storedSize={r.chunk_size ?? DEFAULT_CHUNK_SIZE}
            storedOverlap={r.chunk_overlap ?? DEFAULT_CHUNK_OVERLAP}
            onParse={(size, overlap) => void handleParse(r, size, overlap)}
          />
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
      width={drawerWidth}
      destroyOnClose
      styles={{ body: { padding: 16, position: 'relative' } }}
    >
      {/* 左边缘拖拽调宽手柄：默认隐形，悬停 / 拖拽时浮现 grip；双击重置为默认宽度 */}
      <div
        className="drawer-resizer"
        onMouseDown={onResizeStart}
        onDoubleClick={reset}
        title="拖拽调整宽度（双击重置）"
      >
        <span className="drawer-resizer__grip" />
      </div>
      {kb ? (
        <Flex vertical gap={16}>
          <Typography.Text type="secondary">
            文档 {kb.doc_count} · 索引块 {kb.chunk_count}
            {kb.description ? ` · ${kb.description}` : ''}
          </Typography.Text>

          {/* 上传区（仅注册；参数配置与解析在下方「待解析文件」面板） */}
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
              支持多文件；仅 pdf/docx，上传后在下方「待解析文件」中为各文件设置切块参数并解析
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

          {/* 待解析文件配置面板（存在 pending 文档时出现；解析开始后自动移出） */}
          {pendingDocs.length > 0 ? (
            <PendingParsePanel
              docs={pendingDocs}
              busy={parseDoc.isPending}
              onParse={(doc, size, overlap) => handleParse(doc, size, overlap)}
              onDelete={(doc) => deleteDoc.mutate(doc.id)}
            />
          ) : null}

          {/* 文档表（解析中/已解析/失败） */}
          <Table<KbDocumentOut>
            rowKey="id"
            size="small"
            loading={isLoading}
            columns={columns}
            dataSource={parsedDocs}
            pagination={false}
            scroll={{ x: 1000 }}
            locale={{
              emptyText: isError
                ? '加载失败'
                : pendingDocs.length
                  ? '暂无已解析文档；待解析文件见上方'
                  : '暂无文档，上传第一个吧',
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

/** 待解析文件配置面板：
 *  - 头部「统一参数」（chunk size / overlap）+「应用到全部」+「全部解析」（按各行当前值顺序投递）
 *  - 每文件一行：文件信息 + 独立参数输入（优先级：行输入 > 已存值 > 统一参数 > 全局默认）+ 单独解析/删除
 *  - 维度重置的文档（status=pending 且带已存参数）回面板时会预填其历史参数 */
function PendingParsePanel({
  docs,
  busy,
  onParse,
  onDelete,
}: {
  docs: KbDocumentOut[]
  busy: boolean
  onParse: (doc: KbDocumentOut, size?: number | null, overlap?: number | null) => Promise<boolean>
  onDelete: (doc: KbDocumentOut) => void
}): ReactNode {
  // 统一参数（null=未设置；作为各行占位与「应用」来源）
  const [unifiedSize, setUnifiedSize] = useState<number | null>(null)
  const [unifiedOverlap, setUnifiedOverlap] = useState<number | null>(null)
  // 每文件覆盖（键存在=用户已干预该行；值为 null 表示清空→回退占位默认）
  const [overrides, setOverrides] = useState<Record<UUID, ChunkParams>>({})

  const sizeHint = unifiedSize ?? DEFAULT_CHUNK_SIZE
  const overlapHint = unifiedOverlap ?? DEFAULT_CHUNK_OVERLAP

  const rowValues = (d: KbDocumentOut): ChunkParams => {
    const ov = overrides[d.id]
    return {
      size: ov ? ov.size : (d.chunk_size ?? null),
      overlap: ov ? ov.overlap : (d.chunk_overlap ?? null),
    }
  }

  const setRow = (id: UUID, patch: Partial<ChunkParams>) => {
    setOverrides((prev) => {
      const cur: ChunkParams = prev[id] ?? { size: null, overlap: null }
      return { ...prev, [id]: { ...cur, ...patch } }
    })
  }

  const applyToAll = () => {
    setOverrides(
      Object.fromEntries(
        docs.map((d) => [d.id, { size: unifiedSize, overlap: unifiedOverlap }])
      ) as Record<UUID, ChunkParams>
    )
  }

  const parseAll = async () => {
    // 顺序投递各行（按行当前值；空=后端沿用已存/全局默认），避免挤占解析并发
    let ok = 0
    for (const d of docs) {
      const { size, overlap } = rowValues(d)
      if (await onParse(d, size, overlap)) ok += 1
    }
    if (ok) toastSuccess(`已投递 ${ok}/${docs.length} 个文件解析`)
  }

  return (
    <div
      style={{
        border: '1px solid var(--color-border)',
        borderRadius: 8,
        padding: '12px 14px 4px',
        background: 'var(--color-bg-hover, rgba(0,0,0,0.02))',
      }}
    >
      {/* 头部：说明 + 统一参数 + 批量操作 */}
      <Flex justify="space-between" align="flex-start" gap={12} wrap style={{ paddingBottom: 10 }}>
        <Flex vertical>
          <Typography.Text strong>
            待解析文件（{docs.length}）
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            逐个确认切块参数后解析；留空使用默认 {sizeHint} / {overlapHint}
          </Typography.Text>
        </Flex>
        <Flex gap={8} align="center" wrap>
          <InputNumber
            min={200}
            max={8000}
            precision={0}
            value={unifiedSize ?? undefined}
            placeholder={`统一切块 ${sizeHint}`}
            onChange={(v) => setUnifiedSize(typeof v === 'number' ? v : null)}
            style={{ width: 130 }}
            disabled={busy}
          />
          <InputNumber
            min={0}
            max={2000}
            precision={0}
            value={unifiedOverlap ?? undefined}
            placeholder={`统一重叠 ${overlapHint}`}
            onChange={(v) => setUnifiedOverlap(typeof v === 'number' ? v : null)}
            style={{ width: 130 }}
            disabled={busy}
          />
          <Button size="small" onClick={applyToAll} disabled={busy}>
            应用到全部
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<Play size={14} />}
            loading={busy}
            onClick={() => void parseAll()}
          >
            全部解析
          </Button>
        </Flex>
      </Flex>

      {/* 文件行：信息 + 独立参数 + 操作 */}
      {docs.map((d) => {
        const { size, overlap } = rowValues(d)
        return (
          <Flex
            key={d.id}
            align="center"
            gap={12}
            wrap
            style={{
              padding: '10px 0',
              borderTop: '1px solid var(--color-border)',
            }}
          >
            <Flex vertical gap={2} style={{ flex: '1 1 260px', minWidth: 0 }}>
              <Space size={6}>
                <FileText size={14} style={{ flexShrink: 0, color: 'var(--color-text-tertiary)' }} />
                <Tooltip title={d.filename}>
                  <span
                    style={{
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      maxWidth: 320,
                      fontWeight: 500,
                    }}
                  >
                    {d.filename}
                  </span>
                </Tooltip>
                <Tag style={{ marginInlineStart: 0 }}>{fileExtTag(d.filename)}</Tag>
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {formatBytes(d.size)} · {d.page_count != null ? `${d.page_count} 页/段` : '页数未知'}
              </Typography.Text>
            </Flex>
            <InputNumber
              min={200}
              max={8000}
              precision={0}
              value={size ?? undefined}
              placeholder={`切块 ${sizeHint}`}
              onChange={(v) => setRow(d.id, { size: typeof v === 'number' ? v : null })}
              style={{ width: 110 }}
              disabled={busy}
            />
            <InputNumber
              min={0}
              max={2000}
              precision={0}
              value={overlap ?? undefined}
              placeholder={`重叠 ${overlapHint}`}
              onChange={(v) => setRow(d.id, { overlap: typeof v === 'number' ? v : null })}
              style={{ width: 110 }}
              disabled={busy}
            />
            <Tooltip title="按当前参数解析此文件">
              <Button
                type="link"
                size="small"
                icon={<Play size={14} />}
                disabled={busy}
                onClick={() => void onParse(d, size, overlap)}
              />
            </Tooltip>
            <Popconfirm
              title="删除该文档？"
              onConfirm={() => onDelete(d)}
              okText="删除"
              cancelText="取消"
            >
              <Button type="link" size="small" danger icon={<Trash2 size={14} />} />
            </Popconfirm>
          </Flex>
        )
      })}
    </div>
  )
}

/** 重新解析按钮 + 参数 Popover（文档表内，indexed/failed 用；空=沿用已存值） */
function ParsePopover({
  loading,
  disabled,
  storedSize,
  storedOverlap,
  onParse,
}: {
  loading: boolean
  disabled: boolean
  storedSize: number
  storedOverlap: number
  onParse: (chunkSize?: number | null, chunkOverlap?: number | null) => void
}): ReactNode {
  const [open, setOpen] = useState(false)
  const [size, setSize] = useState<number | null>(null)
  const [overlap, setOverlap] = useState<number | null>(null)

  const submit = () => {
    setOpen(false)
    onParse(size, overlap)
  }

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      title="重新解析"
      content={
        <Flex vertical gap={8} style={{ width: 240 }}>
          <label style={{ fontSize: 12 }}>
            切块大小（chunk size，当前 {storedSize}）
            <InputNumber
              min={200}
              max={8000}
              precision={0}
              value={size ?? undefined}
              placeholder={`沿用 ${storedSize}`}
              onChange={(v) => setSize(typeof v === 'number' ? v : null)}
              style={{ width: '100%', marginTop: 2 }}
            />
          </label>
          <label style={{ fontSize: 12 }}>
            重叠（overlap，当前 {storedOverlap}）
            <InputNumber
              min={0}
              max={2000}
              precision={0}
              value={overlap ?? undefined}
              placeholder={`沿用 ${storedOverlap}`}
              onChange={(v) => setOverlap(typeof v === 'number' ? v : null)}
              style={{ width: '100%', marginTop: 2 }}
            />
          </label>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            重新解析将替换该文档全部索引块；留空沿用当前参数
          </Typography.Text>
          <Button type="primary" size="small" loading={loading} onClick={submit}>
            重新解析
          </Button>
        </Flex>
      }
    >
      <Tooltip title={disabled ? '解析中' : '重新解析（可调切块参数）'}>
        <Button type="link" size="small" icon={<Play size={14} />} disabled={disabled} />
      </Tooltip>
    </Popover>
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
