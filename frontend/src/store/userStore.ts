/* 当前用户（单一身份入口）—— 登录态由 httpOnly cookie 会话令牌承载，
   /auth/me 启动探测后落此 store；userId（= user.id，uuid）为既有消费方的
   兼容读取面。身份服务端推导：客户端不再传 user_id，localStorage 不落任何
   身份信息（退出/过期即净空）。 */
import { create } from 'zustand'
import { authApi } from '@/api/auth'
import type { AuthUser } from '@/types/auth'

type AuthStatus = 'loading' | 'authenticated' | 'guest'

interface AuthState {
  user: AuthUser | null
  status: AuthStatus
  /** 兼容面：既有消费方按字符串 user_id 使用（= user.id） */
  userId: string | null
  /** 启动探测（App 挂载时调用一次）；401 → guest，其余错误保持 guest 不阻断 */
  fetchMe: () => Promise<void>
  setUser: (user: AuthUser) => void
  clearUser: () => void
}

export const useCurrentUser = create<AuthState>((set) => ({
  user: null,
  status: 'loading',
  userId: null,
  fetchMe: async () => {
    try {
      const user = await authApi.me()
      set({ user, userId: user.id, status: 'authenticated' })
    } catch {
      set({ user: null, userId: null, status: 'guest' })
    }
  },
  setUser: (user) => set({ user, userId: user.id, status: 'authenticated' }),
  clearUser: () => set({ user: null, userId: null, status: 'guest' }),
}))
