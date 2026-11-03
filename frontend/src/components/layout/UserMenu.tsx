/* 当前用户菜单 —— 侧边栏底部身份芯片（登录态）：昵称常驻 + 管理员徽标，
    Popover 承载个人中心 / 系统管理（管理员）/ 退出登录。
    原多身份切换器随登录体系退役：身份唯一且服务端推导。 */
import { Avatar, Badge, Popover, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { Check, ChevronDown, KeyRound, LogOut, ShieldCheck, UserRound } from 'lucide-react'
import { authApi } from '@/api/auth'
import { useCurrentUser } from '@/store/userStore'

function MenuRow({
  icon,
  label,
  hint,
  onClick,
  danger,
}: {
  icon: ReactNode
  label: string
  hint?: string
  onClick: () => void
  danger?: boolean
}) {
  return (
    <button
      type="button"
      className="user-menu-row"
      onClick={onClick}
      style={{ color: danger ? 'var(--color-danger)' : undefined }}
    >
      <span style={{ display: 'inline-flex', color: 'inherit' }}>{icon}</span>
      <span style={{ flex: 1, textAlign: 'left', fontSize: 13 }}>{label}</span>
      {hint ? (
        <span style={{ fontSize: 12, color: 'var(--color-text-tertiary)' }}>{hint}</span>
      ) : null}
    </button>
  )
}

export function UserMenu({ collapsed = false }: { collapsed?: boolean }): ReactNode {
  const navigate = useNavigate()
  const user = useCurrentUser((s) => s.user)
  const clearUser = useCurrentUser((s) => s.clearUser)
  if (!user) return null

  const handleLogout = async () => {
    try {
      await authApi.logout()
    } catch {
      /* 会话已失效也照常回登录页 */
    }
    clearUser()
    navigate('/login', { replace: true })
  }

  const initial = user.nickname.trim()[0]?.toUpperCase() ?? '?'
  const chip = (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: collapsed ? 0 : '0 4px',
        cursor: 'pointer',
        minWidth: 0,
      }}
    >
      <Badge dot={user.is_manager} offset={[-2, 24]} title="管理员" color="var(--color-primary)">
        {collapsed ? (
          <Avatar size={26} style={{ background: 'var(--color-primary-bg)', color: 'var(--color-primary)', fontWeight: 700 }}>
            {initial}
          </Avatar>
        ) : (
          <Avatar
            size={26}
            style={{ background: 'var(--color-primary)', color: '#fff', fontWeight: 700 }}
          >
            {initial}
          </Avatar>
        )}
      </Badge>
      {collapsed ? null : (
        <>
          <span style={{ fontWeight: 600, fontSize: 13, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.nickname}
          </span>
          <ChevronDown size={13} style={{ color: 'var(--color-text-tertiary)', flexShrink: 0 }} />
        </>
      )}
    </span>
  )

  return (
    <Popover
      trigger="click"
      placement={collapsed ? 'right' : 'top'}
      arrow={false}
      styles={{ body: { padding: 6, minWidth: 190 } }}
      content={
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ padding: '4px 8px 8px', borderBottom: '1px solid var(--color-border-soft)', marginBottom: 4 }}>
            <Typography.Text strong style={{ fontSize: 13 }}>
              {user.nickname}
            </Typography.Text>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                @{user.username}
                {user.is_manager ? ' · 管理员' : ''}
              </Typography.Text>
            </div>
          </div>
          <MenuRow icon={<UserRound size={14} />} label="个人中心" onClick={() => navigate('/profile')} />
          {user.is_manager ? (
            <MenuRow icon={<ShieldCheck size={14} />} label="系统管理" onClick={() => navigate('/admin')} />
          ) : null}
          <MenuRow icon={<KeyRound size={14} />} label="修改密码" hint="个人中心" onClick={() => navigate('/profile')} />
          <MenuRow icon={<LogOut size={14} />} label="退出登录" danger onClick={handleLogout} />
        </div>
      }
    >
      {chip}
    </Popover>
  )
}

export function CurrentUserRoleTag(): ReactNode {
  const user = useCurrentUser((s) => s.user)
  if (!user?.is_manager) return null
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        fontSize: 11,
        color: 'var(--color-primary)',
        background: 'var(--color-primary-bg)',
        borderRadius: 999,
        padding: '1px 7px',
      }}
    >
      <Check size={11} /> 管理员
    </span>
  )
}
