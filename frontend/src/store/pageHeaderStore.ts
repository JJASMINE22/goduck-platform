/* 页面标题上提（page header lift-up）—— 各页面把「标题/副标题/动作」注册到
   顶部 Header 卡（Topbar 渲染），内容卡退化为纯列表/工作区，消除卡内标题行。
    - PageContainer / ChatPage / AgentEditorPage 在渲染 effect 中 setHeader，
      卸载时置 null（路由切换同一批次内完成，无空窗闪烁）。
    - title/extra 为 ReactNode：可携带返回按钮、状态 Tag、Segmented 等动态内容；
      注册方每次渲染重设，闭包内的回调始终新鲜。 */
import { create } from 'zustand'
import type { ReactNode } from 'react'

export interface PageHeaderInfo {
  title: ReactNode
  subtitle?: ReactNode
  extra?: ReactNode
}

interface PageHeaderState {
  header: PageHeaderInfo | null
  setHeader: (h: PageHeaderInfo | null) => void
}

export const usePageHeaderStore = create<PageHeaderState>((set) => ({
  header: null,
  setHeader: (header) => set({ header }),
}))
