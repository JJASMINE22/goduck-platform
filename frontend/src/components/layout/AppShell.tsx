/* 应用外壳 —— AntD Layout 组合 Sider + Header + Content。
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
    <Layout style={{ height: '100%' }}>
      <Sider
        theme="light"
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        width={220}
        style={{ borderRight: '1px solid var(--color-border-soft)', overflow: 'auto' }}
      >
        <Sidebar />
      </Sider>
      <Layout>
        <Header style={{ padding: 0, height: 56, lineHeight: '56px' }}>
          <Topbar />
        </Header>
        <Content style={{ overflow: 'auto', padding: 20 }}>
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </Content>
      </Layout>
    </Layout>
  )
}
