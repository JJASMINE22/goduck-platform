/* 认证 API —— 对应后端 api/auth.py（prefix /auth）。
   会话凭证为 httpOnly cookie（同源 fetch/SSE/WS 自动携带），前端只管调用。 */
import { del, get, post, put } from './client'
import type { AdminUser, AuthRequestOut, AuthUser, LoginRequest, RegisterRequest } from '@/types/auth'

export const authApi = {
  login: (body: LoginRequest) => post<AuthUser>('/auth/login', body),

  logout: () => post<{ ok: boolean }>('/auth/logout'),

  me: () => get<AuthUser>('/auth/me'),

  register: (body: RegisterRequest) =>
    post<{ ok: boolean; message: string }>('/auth/register', body),

  /** 改昵称（即时查重 + 保留词黑名单，零审批） */
  updateNickname: (nickname: string) => put<AuthUser>('/auth/profile', { nickname }),

  /** 自助改密（验旧密 + 两次确认）；成功即吊销全部会话 → 回登录页 */
  changePassword: (body: { old_password: string; new_password: string; confirm_password: string }) =>
    post<{ ok: boolean; message: string }>('/auth/password', body),

  // ---- 管理端（require_manager）----

  listRequests: (status?: string) =>
    get<AuthRequestOut[]>('/auth/requests', status ? { status } : undefined),

  approveRequest: (id: string) => post<AuthUser>(`/auth/requests/${id}/approve`, {}),

  rejectRequest: (id: string, reason?: string) =>
    post<AuthRequestOut>(`/auth/requests/${id}/reject`, { reason: reason || null }),

  /** 删除被拒申请记录：释放该用户名的注册占位（仅 rejected 行可删） */
  deleteRequest: (id: string) => del<{ ok: boolean }>(`/auth/requests/${id}`),

  listUsers: () => get<AdminUser[]>('/auth/users'),

  setUserState: (id: string, state: 'active' | 'disabled') =>
    post<{ ok: boolean }>(`/auth/users/${id}/state`, { state }),

  unlockUser: (id: string) => post<{ ok: boolean }>(`/auth/users/${id}/unlock`),

  /** 管理员重置密码（原密码不可见；重置后目标全部会话吊销） */
  resetPassword: (id: string, newPassword: string) =>
    post<{ ok: boolean }>(`/auth/users/${id}/password`, {
      new_password: newPassword,
      confirm_password: newPassword,
    }),
}
