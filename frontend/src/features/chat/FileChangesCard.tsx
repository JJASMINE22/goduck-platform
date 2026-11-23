/* 文件变更卡片（聊天时间线）—— git diff --stat 式回合级聚合视图。
    默认折叠一行汇总（落盘 N · 编辑 M · 删除 K，数字为文件数），展开逐文件复刻
    截图样式：[动作图标] 编辑 · ChatPage.tsx  +3 −2。
    点击文件行 → 切到工作区视图并选中该文件（ChatPage.openWorkspaceFile）。
    行尾明细按钮 → 行下展开行级红绿 diff（DiffViewer）；同文件多次落盘按到达序
    逐条展示（聚合计数为合并值，明细保持每次落盘的原始粒度）。 */
import { Collapse, Flex, Tooltip } from 'antd'
import { type ReactNode, memo, useMemo, useState } from 'react'
import { Diff, FileDiff, FileMinus, FilePen, FilePlus } from 'lucide-react'
import { mergeFileChanges, type MergedFileChange } from '@/lib/fileChanges'
import { parseDiff } from './diffResult'
import { DiffViewer } from './DiffViewer'
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
      <FileDiff size={13} style={{ color: 'var(--color-card-files)' }} />
      <span style={{ fontSize: 13 }}>文件变更</span>
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
  raws,
  onOpenFile,
}: {
  change: MergedFileChange
  /** 该 path 的全部原始记录（到达序）：明细逐条渲染的来源 */
  raws: FileChangeEntry[]
  onOpenFile?: (path: string) => void
}): ReactNode {
  const { label, Icon } = ACTION_META[change.action]
  const deleted = change.action === 'delete'
  const clickable = !!onOpenFile && !deleted
  const [showDetail, setShowDetail] = useState(false)
  const detailDiffs = useMemo(
    () => raws.filter((r) => r.diff).map((r) => parseDiff(r.diff!)),
    [raws],
  )
  const hasDetail = detailDiffs.length > 0
  return (
    <>
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
          {hasDetail ? (
            <button
              type="button"
              aria-label="行级明细"
              className={`fc-row__btn ${showDetail ? 'fc-row__btn--active' : ''}`}
              /* 行点击语义是跳工作区；按钮只切换行下明细，阻断冒泡 */
              onClick={(e) => {
                e.stopPropagation()
                setShowDetail((v) => !v)
              }}
            >
              <Diff size={12} />
            </button>
          ) : null}
        </div>
      </Tooltip>
      {showDetail && hasDetail ? (
        <div className="fc-detail">
          {detailDiffs.map((p, i) => (
            <DiffViewer key={i} lines={p.lines} />
          ))}
        </div>
      ) : null}
    </>
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
  const rawsByPath = useMemo(() => {
    const m = new Map<string, FileChangeEntry[]>()
    for (const r of records) {
      const cur = m.get(r.path)
      if (cur) cur.push(r)
      else m.set(r.path, [r])
    }
    return m
  }, [records])
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)' }}>
        <FileDiff size={14} />
      </div>
      <div className="msg-card msg-card--files">
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
                    <FileChangeRow
                      key={m.path}
                      change={m}
                      raws={rawsByPath.get(m.path) ?? []}
                      onOpenFile={onOpenFile}
                    />
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
