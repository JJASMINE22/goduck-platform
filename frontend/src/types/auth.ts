/* 登录认证类型 —— 对应后端 api/auth.py。
   用户名（登录锚点，不可变）与昵称（展示名，即时查重可改）分离；
   身份由 httpOnly cookie 会话令牌承载，前端零存密。 */

export interface AuthUser {
  id: string
  username: string
  nickname: string
  is_manager: boolean
  state: 'active' | 'disabled' | string
  last_login_at?: string | null
  password_updated_at?: string | null
  created_at?: string | null
}

export interface AdminUser extends AuthUser {
  failed_attempts: number
  locked_until?: string | null
  updated_at?: string | null
}

export type AuthRequestStatus = 'pending' | 'approved' | 'rejected'

export interface AuthRequestOut {
  id: string
  status: AuthRequestStatus | string
  reason?: string | null
  username: string
  nickname: string
  created_at: string
  reviewed_at?: string | null
}

export type LoginMode = 'username' | 'nickname'

export interface LoginRequest {
  username: string
  password: string
  mode?: LoginMode
}

export interface RegisterRequest {
  username: string
  nickname: string
  password: string
}
