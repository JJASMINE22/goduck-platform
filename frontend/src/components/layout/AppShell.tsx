/* 应用外壳 —— AntD Layout 组合 Sider + Header + Content。
   浮层模块布局：画布（冷灰）上留 10px 间隙，Sider / Header 为流体玻璃卡浮于其上
   （.app-glass：半透 + backdrop-filter + rim 高光），模块间靠留隙 + 背景对比隔离。
   画布上的环境光层（.app-ambient）为玻璃提供可折射的"景"——无遮挡也有流动光感。
   Content 内挂 <Outlet/> 渲染当前路由。准则 F4：路由级 ErrorBoundary 包裹。 */
import { Layout } from 'antd'
import { Outlet } from 'react-router-dom'
import { useState, type ReactNode } from 'react'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { ErrorBoundary } from '@/components/feedback/ErrorBoundary'

const { Sider, Header, Content } = Layout

export function AppShell(): ReactNode {
  const [collapsed, setCollapsed] = useState(false)
  return (
    <Layout
      className="app-shell"
      style={{ height: '100%', padding: 10, gap: 10, background: 'var(--color-bg)', position: 'relative' }}
    >
      {/* 环境光层：纯装饰光斑（pointer-events:none），玻璃面取景后获得常驻折射感 */}
      <div className="app-ambient" aria-hidden="true" />
      {/* 模块卡提层（z-index:1）浮于光斑（z-index:0）之上 */}
      <Sider
        className="app-sider app-glass"
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={220}
        style={{ overflow: 'hidden', position: 'relative', zIndex: 1 }}
      >
        <Sidebar collapsed={collapsed} />
      </Sider>
      <Layout style={{ gap: 10, background: 'transparent', position: 'relative', zIndex: 1 }}>
        <Header
          className="app-glass"
          style={{ padding: 0, height: 56, lineHeight: '56px', overflow: 'hidden' }}
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
