/* 会话页「工作区」视图（与会话同级的页内页面）—— 全页分栏：文件树 | 预览。
   - 文件树：AntD Tree + showLine（层级连线 + 叶线，接近 tree 命令观感），递归渲染
     （后端已限条目/深度）。
   - 行内悬停操作：下载（文件直链 / 目录 zip）、删除（Popconfirm，递归删目录）。
   - 选中文件 → 右侧分栏 WorkspaceFilePreview（按类型分派）；未选中时树占满全宽，
     选中后树收窄为左栏，宽度可拖拽（panel-resizer）。
   - 删除是用户裁剪历史产物的行为，不影响智能体逻辑；选中文件被删后预览自动关闭。 */
import { Button, Empty, Popconfirm, Skeleton, Space, Tooltip, Tree, Typography } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { type MouseEvent as ReactMouseEvent, type ReactNode, useCallback, useMemo, useState } from 'react'
import { ChevronDown, Download, Folder, File as FileIcon, FolderOpen, RefreshCw, Trash2 } from 'lucide-react'
import { workspaceApi } from '@/api/workspace'
import { useDeleteWorkspaceItem, useWorkspaceTree } from '@/hooks/useWorkspace'
import type { UUID, WorkspaceNode } from '@/types'
import { WorkspaceFilePreview } from './WorkspaceFilePreview'

const { Text } = Typography

const clamp = (min: number, max: number, v: number) => Math.min(max, Math.max(min, v))

/** 文件树栏宽度（选中文件后分栏，拖拽分隔条调整） */
const TREE_WIDTH = { min: 200, max: 560, default: 320 } as const

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

export function WorkspaceView({ sessionId }: { sessionId: UUID }): ReactNode {
  const treeQ = useWorkspaceTree(sessionId, true)
  const deleteMut = useDeleteWorkspaceItem(sessionId)
  const [selected, setSelected] = useState<Selected>(null)
  // 左栏宽度（选中文件后生效；向左拖 clientX 减小 → 树变宽）
  const [treeWidth, setTreeWidth] = useState<number>(TREE_WIDTH.default)

  const onTreeResizeDown = (e: ReactMouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = treeWidth
    const onMove = (ev: MouseEvent) => setTreeWidth(clamp(TREE_WIDTH.min, TREE_WIDTH.max, startW + (startX - ev.clientX)))
    const onUp = () => {
      document.body.style.cursor = ''
      document.removeEventListener('mousemove', onMove)
      document.removeEventListener('mouseup', onUp)
    }
    document.body.style.cursor = 'col-resize'
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
  }

  // 删除为事件驱动（非 effect）：成功后失效树（hook 全局 onSuccess）+ 关闭预览分栏
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
    <div className="ws-view">
      <aside
        className="ws-view__tree"
        style={selected ? { width: treeWidth, flexShrink: 0 } : { flex: 1, minWidth: 0 }}
      >
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
                if (node && !node.is_dir) setSelected({ path: node.path, name: node.name })
              }}
            />
          )}
        </div>
      </aside>

      {selected ? (
        <>
          <div className="panel-resizer" onMouseDown={onTreeResizeDown} title="拖拽调整文件树宽度" />
          <section className="ws-view__preview">
            {/* key=文件路径：切换文件时重挂载，滚动复位且不闪现上一文件内容 */}
            <WorkspaceFilePreview
              key={selected.path}
              sessionId={sessionId}
              path={selected.path}
              name={selected.name}
              onClose={() => setSelected(null)}
            />
          </section>
        </>
      ) : null}
    </div>
  )
}
