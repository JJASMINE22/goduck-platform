/* 会话阅读模式（简洁 | 详细）—— 全局唯一响应式展示偏好源，持久化到 localStorage。
    与 themeStore 同模式的纯客户端偏好：服务端零感知、零落盘。

    语义（仅作用于 MessageList 的渲染投影，事件分发与 chatStore 恒为全量超集）：
    - detailed（缺省）：完整过程观测——工具调用卡 / kb_search 卡照常渲染；
    - concise：时间线隐藏工具调用卡（执行参数/返回观测）；思考块、任务清单、
      用量徽标、文件变更卡、HITL 卡、气泡等其余元素原样展示。

    因为过滤只发生在渲染层，切换即时生效且可回溯（含流式进行中——已到达的
    工具卡数据仍在 store，切回详细即显影），回撤时间窗（removeTurn 的
    createdAt 过滤）等存储层语义不受影响。 */
import { create } from 'zustand'

export type ChatDisplayMode = 'concise' | 'detailed'

const DISPLAY_KEY = 'goduck.chatDisplay'

function initialMode(): ChatDisplayMode {
  try {
    return localStorage.getItem(DISPLAY_KEY) === 'concise' ? 'concise' : 'detailed'
  } catch {
    return 'detailed'
  }
}

const init = initialMode()

interface ChatDisplayState {
  mode: ChatDisplayMode
  setMode: (mode: ChatDisplayMode) => void
}

export const useChatDisplayStore = create<ChatDisplayState>((set) => ({
  mode: init,
  setMode: (mode) => {
    try {
      localStorage.setItem(DISPLAY_KEY, mode)
    } catch {
      /* 隐私模式等存储不可用：仅本次会话生效 */
    }
    set({ mode })
  },
}))
