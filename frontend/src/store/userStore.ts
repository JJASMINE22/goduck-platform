/* 当前用户标识（单一身份入口）—— 全局唯一响应式 user_id 源，持久化到 localStorage。
   Topbar 切换 → 创建智能体(creator)、建会话/聊天(user_id)、附件归属、后端记忆命名空间
   全部跟随。无登录系统，个人/局域网自觉制：user_id 仍是客户端控制，非安全边界。 */
import { create } from 'zustand'

const USER_KEY = 'goduck.user_id'

interface UserState {
  userId: string
  setUserId: (v: string) => void
}

export const useCurrentUser = create<UserState>((set) => ({
  userId: localStorage.getItem(USER_KEY) || 'admin',
  setUserId: (v) => {
    const value = v || 'admin'
    localStorage.setItem(USER_KEY, value)
    set({ userId: value })
  },
}))
