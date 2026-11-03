/* 路由守卫 —— 登录态（RequireAuth）与管理员角色（RequireManager）。
   身份唯一真相 = userStore（/auth/me 探测）；guest 回登录页（记录来路），
   非管理员进管理/编辑路由回 /agents（对应后端 401/403 的前端镜像）。 */
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { PageLoading } from '@/components/feedback/states'
import { useCurrentUser } from '@/store/userStore'

export function RequireAuth({ children }: { children?: ReactNode }): ReactNode {
  const { status } = useCurrentUser()
  const location = useLocation()
  if (status === 'loading') return <PageLoading />
  if (status === 'guest') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return children ?? <Outlet />
}

export function RequireManager({ children }: { children?: ReactNode }): ReactNode {
  const user = useCurrentUser((s) => s.user)
  if (!user) return <Navigate to="/login" replace />
  if (!user.is_manager) return <Navigate to="/agents" replace />
  return children ?? <Outlet />
}
