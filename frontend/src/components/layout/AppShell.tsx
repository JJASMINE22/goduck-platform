/* 应用外壳 —— AntD Layout 组合 Sider + Header + Content。
   浮层模块布局：画布（冷灰）上留 10px 间隙，Sider / Header 为圆角白卡浮于其上，
   模块间靠留隙 + 背景对比隔离（Trae IDE 式模块感）。
   Content 内挂 <Outlet/> 渲染当前路由。准则 F4：路由级 ErrorBoundary 包裹。 */
import { Layout } from 'antd'
import { Outlet } from 'react-router-dom'
import { useState, type ReactNode } from 'react'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { ErrorBoundary } from '@/components/feedback/ErrorBoundary'

const { Sider, Header, Content } = Layout

/** 模块卡片通用外观（与 token 同源：白底 + 细边 + 大圆角 + 软阴影） */
const MODULE_CARD_STYLE = {
  background: '#ffffff',
  border: '1px solid var(--color-border-soft)',
  borderRadius: 14,
  boxShadow: 'var(--shadow-sm)',
} as const

export function AppShell(): ReactNode {
  const [collapsed, setCollapsed] = useState(false)
  return (
    <Layout style={{ height: '100%', padding: 10, gap: 10, background: 'var(--color-bg)' }}>
      <Sider
        className="app-sider"
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={220}
        style={{ ...MODULE_CARD_STYLE, overflow: 'hidden' }}
      >
        <Sidebar collapsed={collapsed} />
      </Sider>
      <Layout style={{ gap: 10, background: 'transparent' }}>
        <Header
          style={{ ...MODULE_CARD_STYLE, padding: 0, height: 56, lineHeight: '56px', overflow: 'hidden' }}
        >
          <Topbar />
        </Header>
        <Content style={{ overflow: 'hidden', padding: 0, background: 'transparent' }}>
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  )
}
