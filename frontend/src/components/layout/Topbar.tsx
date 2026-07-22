/* 顶栏 —— 含后端健康指示灯（useHealth 轮询）。准则 F4：健康可见。 */
import { Flex, Tooltip } from 'antd'
import type { ReactNode } from 'react'
import { useHealth } from '@/hooks/useHealth'

function HealthBadge(): ReactNode {
  const { data, isLoading, isError } = useHealth()
  const ok = !isError && data?.status === 'ok'
  const color = isLoading ? 'var(--color-warning)' : ok ? 'var(--color-success)' : 'var(--color-danger)'
  const text = isLoading ? '检测中' : ok ? '后端在线' : '后端离线'
  return (
    <Tooltip title={text}>
      <Flex align="center" gap={6}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, display: 'inline-block' }} />
        <span style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>{text}</span>
      </Flex>
    </Tooltip>
  )
}

export function Topbar(): ReactNode {
  return (
    <Flex
      align="center"
      justify="space-between"
      style={{ height: '100%', padding: '0 24px', borderBottom: '1px solid var(--color-border-soft)' }}
    >
      <span style={{ color: 'var(--color-text-secondary)', fontSize: 14 }}>
        个人智能体搭建 / 迭代 / 会话工作台
      </span>
      <HealthBadge />
    </Flex>
  )
}
