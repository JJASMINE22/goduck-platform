/* 当前用户标识（单一身份入口）—— 全局唯一响应式 user_id 源，持久化到 localStorage。
   Topbar 切换 → 创建智能体(creator)、建会话/聊天(user_id)、附件归属、后端记忆命名空间
   全部跟随。无登录系统，个人/局域网自觉制：user_id 仍是客户端控制，非安全边界。
   recent：最近用过的 user_id 列表（去重、上限 8），供顶栏账号切换器一点即切；初始化时
   把当前 user_id 种入列表，保证切换器至少有一项可展示。 */
import { create } from 'zustand'

const USER_KEY = 'goduck.user_id'
const RECENT_KEY = 'goduck.recent_users'
const RECENT_CAP = 8

function loadRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

const initialUser = localStorage.getItem(USER_KEY) || 'admin'
const seedRecent = [initialUser, ...loadRecent().filter((u) => u !== initialUser)].slice(0, RECENT_CAP)

interface UserState {
  userId: string
  recent: string[]
  setUserId: (v: string) => void
}

export const useCurrentUser = create<UserState>((set, get) => ({
  userId: initialUser,
  recent: seedRecent,
  setUserId: (v) => {
    const value = v || 'admin'
    localStorage.setItem(USER_KEY, value)
    const recent = [value, ...get().recent.filter((u) => u !== value)].slice(0, RECENT_CAP)
    localStorage.setItem(RECENT_KEY, JSON.stringify(recent))
    set({ userId: value, recent })
  },
}))
