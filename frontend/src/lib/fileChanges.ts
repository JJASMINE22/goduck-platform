/* 文件变更记录的渲染层合并 —— 存储层逐条追加（可精确回撤），展示前按 path 归并：
   同文件多次操作累计 ±N；create 粘滞（create 后再 edit 仍标 create）；delete 终态
   （清计数只显删除）。计数任一来源为 null（二进制/超大）时合并结果不可靠，置 null 不显数字。 */
import type { FileChangeLogItem, FileChangeRecord } from '@/types'
import { sessionsApi } from '@/api/sessions'
import { useChatStore } from '@/store/chatStore'
import type { FileChangeEntry } from '@/store/chatStore'

export interface MergedFileChange {
  path: string
  action: FileChangeRecord['action']
  added: number | null
  removed: number | null
}

/** null（未知计数）参与求和 → 结果也为 null（宁可不显示，不可错显示） */
const sum = (a: number | null, b: number | null): number | null =>
  a == null || b == null ? null : a + b

/** 按到达顺序归并同路径记录；返回顺序 = 首次出现顺序 */
export function mergeFileChanges(records: FileChangeRecord[]): MergedFileChange[] {
  const order: string[] = []
  const map = new Map<string, MergedFileChange>()
  for (const r of records) {
    const cur = map.get(r.path)
    if (!cur) {
      map.set(r.path, { path: r.path, action: r.action, added: r.added, removed: r.removed })
      order.push(r.path)
    } else if (r.action === 'delete') {
      map.set(r.path, { path: r.path, action: 'delete', added: null, removed: null })
    } else {
      map.set(r.path, {
        path: r.path,
        action: cur.action === 'create' ? 'create' : 'edit',
        added: sum(cur.added, r.added),
        removed: sum(cur.removed, r.removed),
      })
    }
  }
  return order.map((p) => map.get(p)!)
}

/** path → MergedFileChange 映射（树徽标 O(1) 命中查找用） */
export function mergeFileChangesByPath(records: FileChangeRecord[]): Map<string, MergedFileChange> {
  return new Map(mergeFileChanges(records).map((m) => [m.path, m]))
}

/** 历史灌入后回放文件变更明细（file_change_log 镜像 → store.fileChanges）。
    锚 = store 末条用户消息 id（服务器消息 id，与落库行 anchor 同源），语义对齐
    live 侧"本回合聚合卡"。best-effort：失败静默（明细回放是增强观测，不阻塞
    历史水合）；灌入前二次校验会话绑定与流式态，防迟到的镜像覆盖 live 增量。 */
export async function hydrateFileChanges(sessionId: string): Promise<void> {
  const messages = useChatStore.getState().messages
  const anchor = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'user') return messages[i].id
    }
    return null
  })()
  if (!anchor) return
  let rows: FileChangeLogItem[]
  try {
    rows = await sessionsApi.fileChanges(sessionId, anchor)
  } catch {
    return
  }
  const st = useChatStore.getState()
  if (st.sessionId !== sessionId || st.status === 'streaming') return
  const entries: FileChangeEntry[] = rows.map((r) => ({
    path: r.path,
    action: r.action,
    added: r.added,
    removed: r.removed,
    diff: r.diff,
    createdAt: new Date(r.created_at).getTime(),
  }))
  st.hydrateFileChanges(sessionId, entries)
}
