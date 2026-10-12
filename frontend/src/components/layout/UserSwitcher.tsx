/* 用户身份切换 —— 账号芯片 + Popover（搜索/新建 + 最近用户一点即切）。
    原挂顶栏右侧，现迁至侧边栏底部（成熟后台惯例：身份常驻导航末端，不占每页
    标题区）；侧栏折叠时退化为纯头像圆钮，Popover 转为向右弹出。
    切换即时生效：创建智能体/会话的身份、后端记忆命名空间均跟随此值。 */
import { Flex, Input, Popover, Typography } from 'antd'
import { useMemo, useState, type ReactNode } from 'react'
import { Check, ChevronDown, Search } from 'lucide-react'
import { useCurrentUser } from '@/store/userStore'

/** 当前用户芯片（solid）：品牌 favicon 缩略图 —— 个人平台无真人头像，以品牌贴纸
    作身份图标；favicon 自带不透明深底，contain 完整缩略、圆容器裁形。
    列表项（非 solid）：首字母浅底头像，多身份仍可相互区分。 */
function UserAvatar({ user, solid = false }: { user: string; solid?: boolean }): ReactNode {
  if (solid) {
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
          overflow: 'hidden',
          background: 'var(--color-primary)', /* 图片加载前的占位底 */
        }}
      >
        <img src="/favicon.png" alt={user} style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
      </span>
    )
  }
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
        background: 'var(--color-primary-bg)',
        color: 'var(--color-primary)',
      }}
    >
      {ch}
    </span>
  )
}

/** 当前用户：账号芯片 + Popover 切换器（搜索/新建 + 最近用户一点即切） */
export function UserSwitcher({ collapsed = false }: { collapsed?: boolean }): ReactNode {
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
      placement={collapsed ? 'right' : 'topLeft'}
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
          e.currentTarget.style.borderColor = 'var(--glass-border-strong)'
          e.currentTarget.style.background = 'var(--glass-bg)'
        }}
        style={
          collapsed
            ? {
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                cursor: 'pointer',
                padding: 4,
                borderRadius: '50%',
                border: '1px solid var(--glass-border-strong)',
                background: 'var(--glass-bg)',
                fontFamily: 'inherit',
                color: 'var(--color-text)',
                transition: 'background .15s, border-color .15s',
              }
            : {
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                cursor: 'pointer',
                width: '100%',
                padding: '5px 8px 5px 6px',
                borderRadius: 'var(--radius-md)',
                border: '1px solid var(--glass-border-strong)',
                background: 'var(--glass-bg)',
                fontFamily: 'inherit',
                color: 'var(--color-text)',
                transition: 'background .15s, border-color .15s',
              }
        }
      >
        <UserAvatar user={userId} solid />
        {collapsed ? null : (
          <>
            <span
              style={{
                fontSize: 13,
                fontWeight: 600,
                flex: '0 1 auto',
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {userId}
            </span>
            <ChevronDown size={14} style={{ color: 'var(--color-text-tertiary)', marginLeft: 'auto', flexShrink: 0 }} />
          </>
        )}
      </button>
    </Popover>
  )
}
