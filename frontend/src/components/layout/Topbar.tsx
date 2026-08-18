/* 顶栏 —— 页面标题区（由各页面经 pageHeaderStore 上提注册）+ 用户身份切换
    （账号芯片/Popover）+ 后端健康指示灯。用户切换即时生效：创建智能体/会话的
    身份、后端记忆命名空间均跟随此值。 */
import { Flex, Input, Popover, Tooltip, Typography } from 'antd'
import { useMemo, useState, type ReactNode } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { useHealth } from '@/hooks/useHealth'
import { useCurrentUser } from '@/store/userStore'
import { usePageHeaderStore } from '@/store/pageHeaderStore'

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

export function Topbar(): ReactNode {
  const header = usePageHeaderStore((s) => s.header)
  return (
    <span className="topbar">
      {header ? (
        <>
          {/* 页面主题：标题 + 副标题（长副标题省略号截断），占据左侧弹性空间 */}
          <span className="topbar-page">
            <span className="topbar-page__title">{header.title}</span>
            {header.subtitle ? (
              <span className="topbar-page__subtitle">{header.subtitle}</span>
            ) : null}
          </span>
          {/* 页面主动作（新建/保存等），紧邻用户区之前 */}
          {header.extra ? <span className="topbar-extra">{header.extra}</span> : null}
        </>
      ) : null}
      <span className="topbar-status">
        <UserSwitcher />
        <HealthBadge />
      </span>
    </span>
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

