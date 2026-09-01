/* 文件变更卡片（聊天时间线）—— git diff --stat 式回合级聚合视图。
    默认折叠一行汇总（落盘 N · 编辑 M · 删除 K，数字为文件数），展开逐文件复刻
    截图样式：[动作图标] 编辑 · ChatPage.tsx  +3 −2。
    点击文件行 → 切到工作区视图并选中该文件（ChatPage.openWorkspaceFile）。 */
import { Collapse, Flex, Tooltip } from 'antd'
import { type ReactNode, memo, useMemo } from 'react'
import { FileDiff, FileMinus, FilePen, FilePlus } from 'lucide-react'
import { mergeFileChanges, type MergedFileChange } from '@/lib/fileChanges'
import type { FileChangeEntry } from '@/store/chatStore'

const ACTION_META: Record<
  MergedFileChange['action'],
  { label: string; Icon: typeof FilePlus }
> = {
  create: { label: '落盘', Icon: FilePlus },
  edit: { label: '编辑', Icon: FilePen },
  delete: { label: '删除', Icon: FileMinus },
}

const basename = (path: string): string => path.split('/').pop() ?? path

function Summary({ merged }: { merged: MergedFileChange[] }): ReactNode {
  const counts = {
    create: merged.filter((m) => m.action === 'create').length,
    edit: merged.filter((m) => m.action === 'edit').length,
    delete: merged.filter((m) => m.action === 'delete').length,
  }
  return (
    <Flex align="center" gap={6}>
      <FileDiff size={13} style={{ color: 'var(--color-role-tool)' }} />
      <span style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>文件变更</span>
      {(Object.keys(counts) as Array<keyof typeof counts>).map((k) =>
        counts[k] > 0 ? (
          <span key={k} className={`fc-summary fc-summary--${k}`}>
            {ACTION_META[k].label} {counts[k]}
          </span>
        ) : null,
      )}
    </Flex>
  )
}

function FileChangeRow({
  change,
  onOpenFile,
}: {
  change: MergedFileChange
  onOpenFile?: (path: string) => void
}): ReactNode {
  const { label, Icon } = ACTION_META[change.action]
  const deleted = change.action === 'delete'
  const clickable = !!onOpenFile && !deleted
  return (
    <Tooltip title={change.path}>
      <div
        className={`fc-row ${deleted ? 'fc-row--deleted' : ''} ${clickable ? 'fc-row--clickable' : ''}`}
        onClick={clickable ? () => onOpenFile!(change.path) : undefined}
      >
        <Icon size={13} className={`fc-row__icon fc-row__icon--${change.action}`} />
        <span className="fc-row__action">{label}</span>
        <span className={`fc-row__path ${deleted ? 'fc-row__path--struck' : ''}`}>
          {basename(change.path)}
        </span>
        {/* 固定宽度数字列（grid 模板）：跨行右对齐；0/null 留空占位保列不塌 */}
        <span className="fc-row__num fc-row__num--add">{change.added ? `+${change.added}` : ''}</span>
        <span className="fc-row__num fc-row__num--del">{change.removed ? `−${change.removed}` : ''}</span>
      </div>
    </Tooltip>
  )
}

/** 文件变更卡（memo：records 引用仅在 file_change 事件追加时变化，流式批
    渲染时跳过） */
export const FileChangesCard = memo(function FileChangesCard({
  records,
  onOpenFile,
}: {
  records: FileChangeEntry[]
  onOpenFile?: (path: string) => void
}): ReactNode {
  const merged = useMemo(() => mergeFileChanges(records), [records])
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)', width: 28, height: 28 }}>
        <FileDiff size={14} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <Collapse
          size="small"
          ghost
          items={[
            {
              key: 'file-changes',
              label: <Summary merged={merged} />,
              children: (
                <div className="fc-list">
                  {merged.map((m) => (
                    <FileChangeRow key={m.path} change={m} onOpenFile={onOpenFile} />
                  ))}
                </div>
              ),
            },
          ]}
        />
      </div>
    </Flex>
  )
})
