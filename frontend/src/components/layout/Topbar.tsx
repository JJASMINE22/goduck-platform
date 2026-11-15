/* 顶栏 —— 页面标题区（由各页面经 pageHeaderStore 上提注册）+ 后端健康指示灯。
    用户身份在侧边栏底部（见 UserMenu），顶栏不再承载用户区。
    紧凑屏（≤768px）：menuSlot 槽位渲染导航入口（AppShell 注入 CompactNav 的
    汉堡钮 + 抽屉，自持开合态）；健康文字随断点隐去只留指示灯（index.css）。 */
import { Tooltip } from 'antd'
import type { ReactNode } from 'react'
import { useHealth } from '@/hooks/useHealth'
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
        <span className="topbar-status__health-text" style={{ color: 'var(--color-text-secondary)', fontSize: 13 }}>
          {text}
        </span>
      </span>
    </Tooltip>
  )
}

export function Topbar({ menuSlot }: { menuSlot?: ReactNode }): ReactNode {
  const header = usePageHeaderStore((s) => s.header)
  return (
    <span className="topbar">
      {menuSlot ?? null}
      {header ? (
        <>
          {/* 页面主题：标题 + 副标题（长副标题省略号截断），占据左侧弹性空间 */}
          <span className="topbar-page">
            <span className="topbar-page__title">{header.title}</span>
            {header.subtitle ? (
              <span className="topbar-page__subtitle">{header.subtitle}</span>
            ) : null}
          </span>
          {/* 页面主动作（新建/保存等），紧邻状态区之前 */}
          {header.extra ? <span className="topbar-extra">{header.extra}</span> : null}
        </>
      ) : null}
      <span className="topbar-status">
        <HealthBadge />
      </span>
    </span>
  )
}
