/* 会话页「工作区」侧面板（plan §3.2）—— 按用户隔离的文件树 + 预览。
   - 文件树：AntD Tree，递归渲染（后端已限条目/深度）。
   - 选中文件 → 抽屉内 WorkspaceFilePreview（按类型分派）。
   - 刷新按钮：agent 写文件后取最新落盘拓扑。
   - 不改全局导航（Sidebar.tsx）；面板职责与会话页内联。 */
import { Button, Drawer, Empty, Skeleton, Space, Tooltip, Tree, Typography } from 'antd'
import type { DataNode } from 'antd/es/tree'
import { type ReactNode, useMemo, useState } from 'react'
import { Folder, FolderOpen, RefreshCw, File as FileIcon } from 'lucide-react'
import { useWorkspaceTree } from '@/hooks/useWorkspace'
import type { UUID, WorkspaceNode } from '@/types'
import { WorkspaceFilePreview } from './WorkspaceFilePreview'

const { Text } = Typography

type Selected = { path: string; name: string } | null

/** WorkspaceNode[] → AntD Tree DataNode[]（递归；目录优先）。 */
function toTreeData(nodes: WorkspaceNode[]): DataNode[] {
  return nodes.map((n) => ({
    key: n.path,
    title: (
      <Space size={4}>
        <span>{n.name}</span>
        {!n.is_dir && n.size != null ? (
          <Text type="secondary" style={{ fontSize: 11 }}>{formatBytes(n.size)}</Text>
        ) : null}
      </Space>
    ),
    icon: n.is_dir ? <Folder size={14} /> : <FileIcon size={13} />,
    isLeaf: !n.is_dir,
    children: n.is_dir && n.children?.length ? toTreeData(n.children) : undefined,
  }))
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}K`
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`
}

export function WorkspacePanel({
  sessionId,
  onClose,
}: {
  sessionId: UUID
  onClose: () => void
}): ReactNode {
  const treeQ = useWorkspaceTree(sessionId, true)
  const [selected, setSelected] = useState<Selected>(null)

  const treeData = useMemo(() => (treeQ.data ? toTreeData(treeQ.data) : []), [treeQ.data])

  return (
    <aside className="ws-panel">
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
          <Button size="small" type="text" onClick={onClose}>收起</Button>
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
        width="min(720px, 70vw)"
        destroyOnClose
        styles={{ body: { padding: 0 } }}
      >
        {selected ? (
          <WorkspaceFilePreview sessionId={sessionId} path={selected.path} name={selected.name} />
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
