/* 会话页「工作区」侧面板（plan §3.2）—— 按用户隔离的文件树 + 预览。
   - 文件树：AntD Tree，递归渲染（后端已限条目/深度）。
   - 行内悬停操作：下载（文件直链 / 目录 zip）、删除（Popconfirm，递归删目录）。
     —— 下载/删除从预览抽屉挪到拓扑层，鼠标悬停行即出现，无需进入预览页。
   - 选中文件 → 抽屉内 WorkspaceFilePreview（按类型分派）。
   - 刷新：agent 写文件后取最新落盘拓扑。
   - 删除是用户裁剪历史产物的行为，不影响智能体逻辑；选中文件被删后抽屉自动关闭。 */
import { Button, Drawer, Empty, Popconfirm, Skeleton, Space, Tooltip, Tree, Typography } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useMemo, useState } from 'react'
import { Download, Folder, FolderOpen, File as FileIcon, PanelRightClose, RefreshCw, Trash2 } from 'lucide-react'
import { workspaceApi } from '@/api/workspace'
import { useDeleteWorkspaceItem, useWorkspaceTree } from '@/hooks/useWorkspace'
import type { UUID, WorkspaceNode } from '@/types'
import { WorkspaceFilePreview } from './WorkspaceFilePreview'

const { Text } = Typography

const clamp = (min: number, max: number, v: number) => Math.min(max, Math.max(min, v))

type Selected = { path: string; name: string } | null

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}K`
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`
}

/** 树节点标题：名称 + 体积 + 悬停操作（下载 / 删除）。 */
function NodeTitle({
  node,
  sessionId,
  onDelete,
}: {
  node: WorkspaceNode
  sessionId: UUID
  onDelete: (n: WorkspaceNode) => void
}): ReactNode {
  return (
    <span className="ws-node">
      <span className="ws-node__label">
        <span className="ws-node__name">{node.name}</span>
        {!node.is_dir && node.size != null ? (
          <span className="ws-node__size">{formatBytes(node.size)}</span>
        ) : null}
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

/** WorkspaceNode[] → AntD Tree DataNode[]（递归；目录优先）。 */
function toTreeData(
  nodes: WorkspaceNode[],
  sessionId: UUID,
  onDelete: (n: WorkspaceNode) => void,
): DataNode[] {
  return nodes.map((n) => ({
    key: n.path,
    title: <NodeTitle node={n} sessionId={sessionId} onDelete={onDelete} />,
    icon: n.is_dir ? <Folder size={14} /> : <FileIcon size={13} />,
    isLeaf: !n.is_dir,
    children: n.is_dir && n.children?.length ? toTreeData(n.children, sessionId, onDelete) : undefined,
  }))
}

export function WorkspacePanel({
  sessionId,
  onClose,
  width = 300,
}: {
  sessionId: UUID
  onClose: () => void
  width?: number
}): ReactNode {
  const treeQ = useWorkspaceTree(sessionId, true)
  const deleteMut = useDeleteWorkspaceItem(sessionId)
  const [selected, setSelected] = useState<Selected>(null)
  // 预览抽屉宽度（左缘拖拉：向左拖加宽）
  const [previewWidth, setPreviewWidth] = useState(560)

  const onPreviewResizeDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const startX = e.clientX
    const startW = previewWidth
    // 向左拖(clientX 减小) → 抽屉变宽
    const onMove = (ev: MouseEvent) => setPreviewWidth(clamp(380, 960, startW + (startX - ev.clientX)))
    const onUp = () => {
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.body.style.cursor = 'col-resize'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  // 删除为事件驱动（非 effect）：成功后失效树（hook 全局 onSuccess）+ 关闭预览抽屉
  // （当被删项正是打开的文件，或被删目录包含该文件时）。mutate 引用稳定（react-query 保证）。
  const handleDelete = useCallback(
    (node: WorkspaceNode) => {
      deleteMut.mutate(node.path, {
        onSuccess: () => {
          setSelected((cur) =>
            cur && (cur.path === node.path || cur.path.startsWith(node.path + '/')) ? null : cur,
          )
        },
      })
    },
    [deleteMut],
  )

  const treeData = useMemo(
    () => (treeQ.data ? toTreeData(treeQ.data, sessionId, handleDelete) : []),
    [treeQ.data, sessionId, handleDelete],
  )

  return (
    <aside className="ws-panel" style={{ width }}>
      <div className="ws-panel__header">
        <Space size={6}>
          <FolderOpen size={15} />
          <Text strong>工作区</Text>
        </Space>
        <Space size={2}>
          <Tooltip title="刷新">
            <Button
              size="small"
              type="text"
              icon={<RefreshCw size={14} />}
              loading={treeQ.isFetching}
              onClick={() => treeQ.refetch()}
            />
          </Tooltip>
          <Tooltip title="收起">
            <Button size="small" type="text" icon={<PanelRightClose size={16} />} onClick={onClose} />
          </Tooltip>
        </Space>
      </div>

      <div className="ws-panel__body">
        {treeQ.isLoading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : treeData.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="工作区为空"
            style={{ marginTop: 32 }}
          />
        ) : (
          <Tree
            showIcon
            blockNode
            treeData={treeData}
            defaultExpandedKeys={rootKeys(treeData)}
            onSelect={(keys) => {
              const key = keys[0]
              if (typeof key !== 'string') return
              // 仅文件可预览（目录仅展开/收起）
              const node = findNode(treeQ.data ?? [], key)
              if (node && !node.is_dir) setSelected({ path: node.path, name: node.name })
            }}
          />
        )}
      </div>

      <Drawer
        title={selected?.name ?? '预览'}
        placement="right"
        open={!!selected}
        onClose={() => setSelected(null)}
        width={previewWidth}
        destroyOnClose
        styles={{ body: { padding: 0, position: 'relative' } }}
      >
        {selected ? (
          <>
            <div
              className="drawer-resizer"
              onMouseDown={onPreviewResizeDown}
              title="拖拽调整预览宽度"
            />
            <WorkspaceFilePreview sessionId={sessionId} path={selected.path} name={selected.name} />
          </>
        ) : null}
      </Drawer>
    </aside>
  )
}

/** 顶层目录 key（用于默认展开首层）。 */
function rootKeys(data: DataNode[]): React.Key[] {
  return data.filter((d) => d.children?.length).map((d) => d.key)
}

/** 在扁平递归树里按 path 查节点。 */
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
