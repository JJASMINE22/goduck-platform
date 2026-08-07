/* 顶栏 —— 路由面包屑（方位感）+ 用户身份切换（账号芯片/Popover）+ 后端健康指示灯。
   用户切换即时生效：创建智能体/会话的身份、后端记忆命名空间均跟随此值。 */
import { Flex, Input, Popover, Tooltip, Typography } from 'antd'
import { Fragment, useMemo, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Check, ChevronDown, ChevronRight, Search } from 'lucide-react'
import { useHealth } from '@/hooks/useHealth'
import { useCurrentUser } from '@/store/userStore'

function HealthBadge(): ReactNode {
  const { data, isLoading, isError } = useHealth()
  const ok = !isError && data?.status === 'ok'
  const color = isLoading ? 'var(--color-warning)' : ok ? 'var(--color-success)' : 'var(--color-danger)'
  const text = isLoading ? '检测中' : ok ? '后端在线' : '后端离线'
  return (
    <Tooltip title={text}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, display: 'inline-block' }} />
        <span style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>{text}</span>
      </span>
    </Tooltip>
  )
}

/** 路由 → 面包屑分段：顶部方位感，与内容区大标题互补（不重复） */
function resolveCrumbs(pathname: string): string[] {
  if (pathname === '/agents' || pathname.startsWith('/agents/')) {
    const seg = pathname === '/agents/new' ? ['新建'] : /^\/agents\/[^/]+$/.test(pathname) ? ['编辑'] : []
    return ['智能体', ...seg]
  }
  if (pathname === '/sessions' || pathname.startsWith('/sessions/')) {
    const seg = /^\/sessions\/[^/]+$/.test(pathname) ? ['对话'] : []
    return ['会话', ...seg]
  }
  if (pathname.startsWith('/settings/models')) return ['设置', '模型']
  if (pathname.startsWith('/settings/mcp')) return ['设置', 'MCP 服务']
  if (pathname.startsWith('/settings/skills')) return ['设置', '技能']
  return []
}

function RouteBreadcrumb(): ReactNode {
  const { pathname } = useLocation()
  const crumbs = resolveCrumbs(pathname)
  if (!crumbs.length) return null
  return (
    <Flex align="center" gap={4}>
      {crumbs.map((c, i) => {
        const last = i === crumbs.length - 1
        return (
          <Fragment key={`${c}-${i}`}>
            {i > 0 && <ChevronRight size={14} style={{ color: 'var(--color-text-tertiary)' }} />}
            <span
              style={{
                fontSize: 14,
                fontWeight: last ? 600 : 400,
                color: last ? 'var(--color-text)' : 'var(--color-text-tertiary)',
              }}
            >
              {c}
            </span>
          </Fragment>
        )
      })}
    </Flex>
  )
}

/** 首字母头像：solid 用主色实底（当前用户芯片），默认浅底（列表项） */
function UserAvatar({ user, solid = false }: { user: string; solid?: boolean }): ReactNode {
  const ch = (user.trim()[0] || '?').toUpperCase()
  return (
    <span
      style={{
        width: 22,
        height: 22,
        borderRadius: '50%',
        flexShrink: 0,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 11,
        fontWeight: 700,
        background: solid ? 'var(--color-primary)' : 'var(--color-primary-bg)',
        color: solid ? '#fff' : 'var(--color-primary)',
      }}
    >
      {ch}
    </span>
  )
}

/** 当前用户：账号芯片 + Popover 切换器（搜索/新建 + 最近用户一点即切） */
function UserSwitcher(): ReactNode {
  const userId = useCurrentUser((s) => s.userId)
  const recent = useCurrentUser((s) => s.recent)
  const setUserId = useCurrentUser((s) => s.setUserId)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')

  // 搜索过滤：无词返回全部最近用户；有词按包含匹配
  const list = useMemo(() => {
    const q = draft.trim().toLowerCase()
    return q ? recent.filter((u) => u.toLowerCase().includes(q)) : recent
  }, [recent, draft])

  const commit = (v: string) => {
    setUserId(v)
    setDraft('')
    setOpen(false)
  }

  const content = (
    <div style={{ width: 248 }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        切换用户
      </Typography.Text>
      <Input
        autoFocus
        allowClear
        size="small"
        prefix={<Search size={13} />}
        placeholder="搜索 / 新建用户，回车确认"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onPressEnter={() => commit(draft)}
        style={{ margin: '8px 0' }}
      />
      <div style={{ maxHeight: 220, overflow: 'auto' }}>
        {list.length ? (
          list.map((u) => {
            const active = u === userId
            return (
              <div
                key={u}
                onClick={() => commit(u)}
                onMouseEnter={(e) => {
                  if (!active) e.currentTarget.style.background = 'var(--color-border-soft)'
                }}
                onMouseLeave={(e) => {
                  if (!active) e.currentTarget.style.background = 'transparent'
                }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  padding: '6px 8px',
                  borderRadius: 'var(--radius-sm)',
                  cursor: 'pointer',
                  background: active ? 'var(--color-primary-bg)' : 'transparent',
                }}
              >
                <Flex align="center" gap={8}>
                  <UserAvatar user={u} />
                  <span style={{ fontSize: 13, fontWeight: active ? 600 : 400 }}>{u}</span>
                </Flex>
                {active ? <Check size={14} style={{ color: 'var(--color-primary)' }} /> : null}
              </div>
            )
          })
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {draft.trim() ? `回车新建「${draft.trim()}」` : '暂无历史用户'}
          </Typography.Text>
        )}
      </div>
      <Typography.Text style={{ fontSize: 11, color: 'var(--color-text-tertiary)', display: 'block', marginTop: 8 }}>
        身份决定智能体/会话归属与记忆隔离
      </Typography.Text>
    </div>
  )

  return (
    <Popover
      trigger="click"
      placement="bottomRight"
      open={open}
      onOpenChange={(o) => {
        setOpen(o)
        if (o) setDraft('')
      }}
      content={content}
    >
      <button
        type="button"
        onMouseEnter={(e) => {
          e.currentTarget.style.borderColor = 'var(--color-primary)'
          e.currentTarget.style.background = 'var(--color-primary-bg)'
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.borderColor = 'var(--color-border)'
          e.currentTarget.style.background = 'var(--color-bg-elevated)'
        }}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 8,
          cursor: 'pointer',
          padding: '3px 10px 3px 5px',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-border)',
          background: 'var(--color-bg-elevated)',
          fontFamily: 'inherit',
          color: 'var(--color-text)',
          transition: 'background .15s, border-color .15s',
        }}
      >
        <UserAvatar user={userId} solid />
        <span style={{ fontSize: 13, fontWeight: 600 }}>{userId}</span>
        <ChevronDown size={14} style={{ color: 'var(--color-text-tertiary)' }} />
      </button>
    </Popover>
  )
}

export function Topbar(): ReactNode {
  return (
    <span
      style={{
        height: '100%',
        padding: '0 24px',
        borderBottom: '1px solid var(--color-border-soft)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}
    >
      <RouteBreadcrumb />
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 20 }}>
        <UserSwitcher />
        <HealthBadge />
      </span>
    </span>
  )
}
