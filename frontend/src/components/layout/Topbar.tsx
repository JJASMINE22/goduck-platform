/* 顶栏 —— 当前用户标识（单一身份入口）+ 后端健康指示灯。
   用户切换即时生效：创建智能体/会话的身份、后端记忆命名空间均跟随此值。 */
import { Input, Tooltip } from 'antd'
import { useState, type ReactNode } from 'react'
import { User } from 'lucide-react'
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

/** 当前用户切换：输入即时改 draft，Enter/blur 提交（空值回落 admin） */
function UserSwitcher(): ReactNode {
  const userId = useCurrentUser((s) => s.userId)
  const setUserId = useCurrentUser((s) => s.setUserId)
  const [prevUserId, setPrevUserId] = useState(userId)
  const [draft, setDraft] = useState(userId)
  // 外部（其它页面）变更同步到 draft —— 渲染期条件调整，避免 effect
  if (userId !== prevUserId) {
    setPrevUserId(userId)
    setDraft(userId)
  }

  const commit = () => {
    const v = draft.trim()
    const value = v || 'admin'
    setUserId(value)
    setDraft(value)
  }

  return (
    <Tooltip title="当前用户：创建智能体(creator)/会话的身份，记忆按此隔离">
      <Input
        size="small"
        prefix={<User size={13} />}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onPressEnter={commit}
        onBlur={commit}
        style={{ width: 190 }}
      />
    </Tooltip>
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
      <span style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
        个人智能体搭建 / 迭代 / 会话工作台
      </span>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 20 }}>
        <UserSwitcher />
        <HealthBadge />
      </span>
    </span>
  )
}
