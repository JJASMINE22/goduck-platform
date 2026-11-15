/* 会话页「工作区」视图（与会话同级的页内页面）—— 树卡全宽 + 预览浮层遮挡：
    - 文件树：AntD Tree + showLine（层级连线 + 叶线，接近 tree 命令观感），递归渲染
      （后端已限条目/深度）。
    - 行内悬停操作：下载（文件直链 / 目录 zip）、删除（Popconfirm，递归删目录）。
    - 选中文件 → 右侧浮起预览卡（按类型分派）覆盖在树卡右段之上：树始终全宽、
      排版恒定（不因打开/拖拽而重排换行），拖拽仅改变遮挡范围，可拖至近全宽
      （左侧留 TREE_REVEAL 窄缝，上限随容器宽动态变化）；双击重置宽度。
    - 选中态由 ChatPage 受控注入（selected/onSelect）：聊天时间线"文件变更卡片"
      点击文件行可一步切到工作区并选中。
    - 树节点徽标：订阅 chatStore.fileChanges，本回合被 agent 落盘/编辑/删除的
      节点显示 +N −M / 绿点 / 红点（新回合开始自动消失）。
    - 删除是用户裁剪历史产物的行为，不影响智能体逻辑；选中文件被删后预览自动关闭。 */
import { Button, Empty, Popconfirm, Skeleton, Space, Tooltip, Tree, Typography } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, Download, Folder, File as FileIcon, FolderOpen, RefreshCw, Trash2 } from 'lucide-react'
import { workspaceApi } from '@/api/workspace'
import { useDeleteWorkspaceItem, useWorkspaceTree } from '@/hooks/useWorkspace'
import { useDragResize } from '@/hooks/useDragResize'
import { useChatStore } from '@/store/chatStore'
import { mergeFileChangesByPath, type MergedFileChange } from '@/lib/fileChanges'
import type { UUID, WorkspaceNode } from '@/types'
import { WorkspaceFilePreview } from './WorkspaceFilePreview'

const { Text } = Typography

/** 预览浮层宽度下限与默认值；上限为动态值（容器宽 - TREE_REVEAL），max 仅作
    测量前的保守兜底 */
const PREVIEW_WIDTH = { min: 320, max: 1600, default: 480 } as const
/** 浮层拖到最宽时左侧保留的树卡窄缝：边界线与拖拽手柄始终可见、可拖回 */
const TREE_REVEAL = 64

/** 选中文件（受控：由 ChatPage 持有，跨视图切换/卡片跳转共享） */
export interface WorkspaceSelection {
  path: string
  name: string
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}K`
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`
}

/** 时间戳短格式：同年 MM-DD HH:mm，跨年回退 YYYY-MM-DD（后端 st_mtime 本地时间无
    时区偏移，直接截取即所见；与预览浮层 DownloadBar 全格式同源口径）。 */
function formatTime(iso: string): string {
  const [date, time] = iso.split('T')
  if (!date) return iso
  if (date.slice(0, 4) === String(new Date().getFullYear())) {
    return time ? `${date.slice(5)} ${time.slice(0, 5)}` : date.slice(5)
  }
  return date
}

/** 变更徽标（树内紧凑变体）：edit=+N −M（省略 0 项）；create=绿点；delete=红点（名称另行淡化）。 */
function ChangeBadge({ change }: { change: MergedFileChange }): ReactNode {
  if (change.action === 'create') {
    return <span className="diff-badge diff-badge--dot diff-badge--add" aria-label="新增" />
  }
  if (change.action === 'delete') {
    return <span className="diff-badge diff-badge--dot diff-badge--del" aria-label="删除" />
  }
  // edit：两项均 null（二进制/超大）→ 不显数字徽标，仅动作语义由名称淡化等承担
  if (change.added == null && change.removed == null) return null
  return (
    <span className="diff-badge diff-badge--group">
      {change.added ? <span className="diff-badge diff-badge--add">+{change.added}</span> : null}
      {change.removed ? <span className="diff-badge diff-badge--del">−{change.removed}</span> : null}
    </span>
  )
}

/** 树节点标题：名称 + 体积 + 变更徽标 + 悬停操作（下载 / 删除）。 */
function NodeTitle({
  node,
  sessionId,
  onDelete,
  badge,
}: {
  node: WorkspaceNode
  sessionId: UUID
  onDelete: (n: WorkspaceNode) => void
  badge?: MergedFileChange
}): ReactNode {
  return (
    <span className={`ws-node ${badge?.action === 'delete' ? 'ws-node--deleted' : ''}`}>
      <span className="ws-node__label">
        <span className="ws-node__name">{node.name}</span>
        {!node.is_dir && node.size != null ? (
          <span className="ws-node__size">{formatBytes(node.size)}</span>
        ) : null}
        {!node.is_dir && node.modified_at ? (
          <span className="ws-node__time">{formatTime(node.modified_at)}</span>
        ) : null}
        {badge ? <ChangeBadge change={badge} /> : null}
      </span>
      <span className="ws-node__actions">
        <Tooltip title="下载">
          <Button
            size="small"
            type="text"
            icon={<Download size={13} />}
            onClick={(e) => {
              e.stopPropagation()
              workspaceApi.download(sessionId, node.path)
            }}
          />
        </Tooltip>
        <Popconfirm
          title={node.is_dir ? '删除该目录？' : '删除该文件？'}
          description={node.is_dir ? '将递归删除目录下所有内容，不可恢复。' : undefined}
          okText="删除"
          okButtonProps={{ danger: true }}
          cancelText="取消"
          onConfirm={() => onDelete(node)}
        >
          <Button
            size="small"
            type="text"
            danger
            icon={<Trash2 size={13} />}
            onClick={(e) => e.stopPropagation()}
          />
        </Popconfirm>
      </span>
    </span>
  )
}

/** WorkspaceNode[] → AntD Tree DataNode[]（递归；目录优先；带变更徽标）。 */
function toTreeData(
  nodes: WorkspaceNode[],
  sessionId: UUID,
  onDelete: (n: WorkspaceNode) => void,
  badges: Map<string, MergedFileChange>,
): DataNode[] {
  return nodes.map((n) => ({
    key: n.path,
    title: <NodeTitle node={n} sessionId={sessionId} onDelete={onDelete} badge={badges.get(n.path)} />,
    icon: n.is_dir ? <Folder size={14} /> : <FileIcon size={13} />,
    isLeaf: !n.is_dir,
    children: n.is_dir && n.children?.length ? toTreeData(n.children, sessionId, onDelete, badges) : undefined,
  }))
}

/** 顶层目录 key（用于默认展开首层）。 */
function rootKeys(data: DataNode[]): React.Key[] {
  return data.filter((d) => d.children?.length).map((d) => d.key)
}

/** 在嵌套树里按 path 查节点。 */
function findNode(nodes: WorkspaceNode[], path: string): WorkspaceNode | undefined {
  for (const n of nodes) {
    if (n.path === path) return n
    if (n.is_dir && n.children) {
      const hit = findNode(n.children, path)
      if (hit) return hit
    }
  }
  return undefined
}

export function WorkspaceView({
  sessionId,
  selected,
  onSelect,
}: {
  sessionId: UUID
  /** 受控选中态（ChatPage 持有：时间线文件变更卡片可跨视图跳转选中） */
  selected: WorkspaceSelection | null
  onSelect: (s: WorkspaceSelection | null) => void
}): ReactNode {
  const treeQ = useWorkspaceTree(sessionId, true)
  const deleteMut = useDeleteWorkspaceItem(sessionId)
  // 本回合文件变更 → 树徽标（store 清空即失：新回合开始徽标自动消失）
  const fileChanges = useChatStore((s) => s.fileChanges)
  const badges = useMemo(() => mergeFileChangesByPath(fileChanges), [fileChanges])
  // 预览浮层宽度：手柄在浮层左缘（edge='left'，向左拖遮得更宽）；双击重置；
  // 宽度持久化跨刷新保留。树卡恒为全宽，排版不随打开/拖拽变化。
  // 上限动态：可拖至近全宽（容器宽 - TREE_REVEAL，左侧留窄缝），由 ResizeObserver
  // 测量 .ws-view 宽度得出；测量前用 PREVIEW_WIDTH.max 兜底。
  const viewRef = useRef<HTMLDivElement | null>(null)
  const [viewWidth, setViewWidth] = useState(0)
  useEffect(() => {
    const el = viewRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) setViewWidth(entry.contentRect.width)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const maxPreview =
    viewWidth > 0
      ? Math.max(PREVIEW_WIDTH.min, Math.round(viewWidth - TREE_REVEAL))
      : PREVIEW_WIDTH.max
  const {
    size: previewWidth,
    onResizeStart: onPreviewResizeStart,
    reset: resetPreviewWidth,
    apply: applyPreviewWidth,
  } = useDragResize({
    initial: PREVIEW_WIDTH.default,
    min: PREVIEW_WIDTH.min,
    max: maxPreview,
    edge: 'left',
    storageKey: 'ws-preview-width',
  })
  // 容器变窄（窗口缩放）或旧持久化值超新上限时收敛：保证状态值 = 实际显示宽
  useEffect(() => {
    if (previewWidth > maxPreview) applyPreviewWidth(maxPreview)
  }, [previewWidth, maxPreview, applyPreviewWidth])

  // 删除为事件驱动（非 effect）：成功后失效树（hook 全局 onSuccess）+ 关闭预览分栏
  // （当被删项正是打开的文件，或被删目录包含该文件时）。mutate 引用稳定（react-query 保证）。
  const handleDelete = useCallback(
    (node: WorkspaceNode) => {
      deleteMut.mutate(node.path, {
        onSuccess: () => {
          onSelect(
            selected && (selected.path === node.path || selected.path.startsWith(node.path + '/'))
              ? null
              : selected,
          )
        },
      })
    },
    [deleteMut, onSelect, selected],
  )

  const treeData = useMemo(
    () => (treeQ.data ? toTreeData(treeQ.data, sessionId, handleDelete, badges) : []),
    [treeQ.data, sessionId, handleDelete, badges],
  )

  return (
    <div className="ws-view" ref={viewRef}>
      <aside className="ws-view__tree" style={{ flex: 1, minWidth: 0 }}>
        <div className="ws-view__tree-header">
          <Space size={6}>
            <FolderOpen size={15} />
            <Text strong>工作区</Text>
          </Space>
          <Tooltip title="刷新">
            <Button
              size="small"
              type="text"
              icon={<RefreshCw size={14} />}
              loading={treeQ.isFetching}
              onClick={() => treeQ.refetch()}
            />
          </Tooltip>
        </div>

        <div className="ws-view__tree-body">
          {treeQ.isLoading ? (
            <Skeleton active paragraph={{ rows: 8 }} />
          ) : treeData.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="工作区为空"
              style={{ marginTop: 48 }}
            />
          ) : (
            <Tree
              showLine={{ showLeafIcon: false }}
              showIcon
              blockNode
              treeData={treeData}
              defaultExpandedKeys={rootKeys(treeData)}
              selectedKeys={selected ? [selected.path] : []}
              switcherIcon={
                <span className="ws-view__switcher">
                  <ChevronDown size={12} />
                </span>
              }
              onSelect={(keys) => {
                const key = keys[0]
                if (typeof key !== 'string') return
                // 仅文件可预览（目录仅展开/收起，不进入选中态）
                const node = findNode(treeQ.data ?? [], key)
                if (node && !node.is_dir) onSelect({ path: node.path, name: node.name })
              }}
            />
          )}
        </div>
      </aside>

      {selected ? (
        <>
          {/* 手柄锚在 .ws-view（而非预览卡内）——预览卡 overflow:hidden 不会裁切探出的
              热区；right 随 previewWidth 联动，10px 热区中心对齐浮层左缘 */}
          <div
            className="panel-resizer ws-view__resizer"
            style={{ right: previewWidth - 5 }}
            onPointerDown={onPreviewResizeStart}
            onDoubleClick={resetPreviewWidth}
            title="拖拽调整预览宽度（双击重置）"
          />
          <section className="ws-view__preview ws-view__preview--float" style={{ width: previewWidth }}>
            {/* key=文件路径：切换文件时重挂载，滚动复位且不闪现上一文件内容 */}
            <WorkspaceFilePreview
              key={selected.path}
              sessionId={sessionId}
              path={selected.path}
              name={selected.name}
              onClose={() => onSelect(null)}
            />
          </section>
        </>
      ) : null}
    </div>
  )
}
