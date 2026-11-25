import { type Key } from 'react'
import { create } from 'zustand'

interface WorkspaceTreeState {
  /** 用户 id → 展开的目录键（树节点 path）；缺省即全折叠 */
  expanded: Partial<Record<string, Key[]>>
  setExpanded: (userId: string, keys: Key[]) => void
}

export const useWorkspaceTreeStore = create<WorkspaceTreeState>((set) => ({
  expanded: {},
  setExpanded: (userId, keys) => set((s) => ({ expanded: { ...s.expanded, [userId]: keys } })),
}))
