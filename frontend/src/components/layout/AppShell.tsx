/* 应用外壳 —— AntD Layout 组合 Sider + Header + Content。
   浮层模块布局：画布（冷灰）上留 10px 间隙，Sider / Header 为流体玻璃卡浮于其上
   （.app-glass：半透 + backdrop-filter + rim 高光），模块间靠留隙 + 背景对比隔离。
   画布上的环境光层（.app-ambient）为玻璃提供可折射的"景"——无遮挡也有流动光感。
   Content 内挂 <Outlet/> 渲染当前路由。准则 F4：路由级 ErrorBoundary 包裹。

   窄视口自适应（断点与 hooks/useMediaQuery.ts 同源；纯展示降级见 index.css
   「窄视口自适应」段）：
   - ≤1100px（narrow）：侧栏自动折叠为图标轨（collapsed = 用户显式意图 ?? 断点
     默认——纯派生无 effect；手动开合固化为意图，跨断点保留）；
   - ≤768px（compact）：侧栏整体改左缘抽屉（CompactNav：汉堡钮 + 抽屉自持开合
     态，断点离开即卸载销毁，无残留重开）；
   - 外壳 padding/gap 走 .app-shell/.app-main 类规则（内联样式无法被断点覆写）。 */
import { Button, Drawer, Layout } from 'antd'
import { useState, type ReactNode } from 'react'
import { Outlet } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { ErrorBoundary } from '@/components/feedback/ErrorBoundary'
import { BREAKPOINTS, useMediaQuery } from '@/hooks/useMediaQuery'

const { Sider, Header, Content } = Layout

/** 紧凑屏导航：汉堡钮（经 Topbar menuSlot 渲染进顶栏行内）+ 侧栏抽屉（Portal
    到 body，不受 Header overflow:hidden 裁切）。开合态自持——断点离开 compact
    时本组件随条件渲染卸载，抽屉状态随之销毁，回到紧凑屏不会残留重开。
    抽屉不渲染 antd 自带头部（closable=false 且无 title 即无 header）：浅色主题下
    头部条与品牌带同色连片，视觉多出一条上带致 logo 显得偏坠；关闭钮（✕）改由
    Sidebar 品牌带左缘承载（onCloseDrawer 传入）。 */
function CompactNav(): ReactNode {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        className="topbar-menu"
        type="text"
        shape="circle"
        aria-label="打开导航"
        icon={<Menu size={17} />}
        onClick={() => setOpen(true)}
      />
      <Drawer
        className="app-drawer-sider"
        placement="left"
        width={288}
        open={open}
        onClose={() => setOpen(false)}
        closable={false}
        styles={{ body: { padding: 0 } }}
      >
        <Sidebar onNavigate={() => setOpen(false)} onCloseDrawer={() => setOpen(false)} />
      </Drawer>
    </>
  )
}

export function AppShell(): ReactNode {
  const isNarrow = useMediaQuery(BREAKPOINTS.narrow)
  const isCompact = useMediaQuery(BREAKPOINTS.compact)
  // 折叠 = 显式用户意图 ?? 断点默认（窄屏折叠 / 宽屏展开）。断点边沿只漂移
  // 默认值；用户手动开合固化为 override 并跨断点保留（意图优先，无需复位）。
  const [collapseOverride, setCollapseOverride] = useState<boolean | null>(null)
  const collapsed = collapseOverride ?? isNarrow

  return (
    <Layout
      className="app-shell"
      style={{ height: '100%', background: 'var(--color-bg)', position: 'relative' }}
    >
      {/* 环境光层：纯装饰光斑（pointer-events:none），玻璃面取景后获得常驻折射感 */}
      <div className="app-ambient" aria-hidden="true" />
      {/* 模块卡提层（z-index:1）浮于光斑（z-index:0）之上。
          折叠由 Logo 旁的侧栏内按钮驱动（不再用 antd 底部默认触发器），
          collapsible 仅渲染触发器、不参与宽度受控，故可安全移除。
          紧凑屏：侧栏让位给 CompactNav 抽屉（汉堡钮在 Topbar） */}
      {isCompact ? null : (
        <Sider
          className="app-sider app-glass"
          collapsed={collapsed}
          width={220}
          style={{ overflow: 'hidden', position: 'relative', zIndex: 1 }}
        >
          <Sidebar collapsed={collapsed} onToggle={() => setCollapseOverride(!collapsed)} />
        </Sider>
      )}
      <Layout
        className="app-main"
        style={{ background: 'transparent', position: 'relative', zIndex: 1 }}
      >
        <Header
          className="app-glass"
          style={{ padding: 0, height: 56, lineHeight: '56px', overflow: 'hidden' }}
        >
          <Topbar menuSlot={isCompact ? <CompactNav /> : undefined} />
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
