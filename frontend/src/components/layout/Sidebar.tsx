/* 侧边导航 —— 对应 DESIGN §9 路由结构。AntD Menu + react-router 导航。
    三级分组标签：应用（业务入口）/ 配置（资源注册）/ 管理（系统，仅管理员）。
    能力驱动（F2）：知识库菜单仅在后端 kb_enabled=true 时出现（单一真相源 /health）；
    管理组按登录角色动态出现。折叠（inlineCollapsed）时分组标签自动隐去，
    菜单项退化为图标 + Tooltip，与底部圆钮（主题/用户）同语言。
    品牌带右侧为侧栏折叠钮（<=|=> 样式，折叠后移至 Logo 正下方）；分组与
    底部工具区不再用分割线，靠留白区隔（保持卡片一体感）。
    底部为用户区（当前用户芯片 + 菜单 Popover）：身份常驻导航末端。 */
import { Bot, BookOpen, Boxes, CalendarClock, Cpu, Database, MessagesSquare, Network, PanelLeftClose, PanelLeftOpen, ShieldCheck } from 'lucide-react'
import { Menu } from 'antd'
import type { MenuProps } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMemo, type ReactNode } from 'react'
import { useCapabilities } from '@/hooks/useCapabilities'
import { useCurrentUser } from '@/store/userStore'
import { ThemeToggle } from './ThemeToggle'
import { UserMenu } from './UserMenu'

type MenuItem = Required<MenuProps>['items'][number]

const icon = (n: ReactNode) => <span style={{ display: 'inline-flex', verticalAlign: 'middle' }}>{n}</span>

/** 全量菜单项；知识库项按能力、管理组按角色动态过滤 */
function buildItems(kbEnabled: boolean, isManager: boolean): MenuItem[] {
  const config: MenuItem[] = [
    { key: '/settings/models', icon: icon(<Cpu size={16} />), label: '模型' },
  ]
  if (kbEnabled) {
    config.push({ key: '/settings/embeddings', icon: icon(<Boxes size={16} />), label: '向量化' })
  }
  config.push(
    { key: '/settings/mcp', icon: icon(<Network size={16} />), label: 'MCP' },
    { key: '/settings/skills', icon: icon(<BookOpen size={16} />), label: '技能' },
  )
  if (kbEnabled) {
    config.push({ key: '/settings/knowledge', icon: icon(<Database size={16} />), label: '知识' })
  }
  const groups: MenuItem[] = [
    {
      type: 'group',
      label: '应用',
      children: [
        { key: '/agents', icon: icon(<Bot size={16} />), label: '智能体' },
        { key: '/sessions', icon: icon(<MessagesSquare size={16} />), label: '会话' },
        { key: '/cron', icon: icon(<CalendarClock size={16} />), label: '定时任务' },
      ],
    },
    { type: 'group', label: '配置', children: config },
  ]
  if (isManager) {
    groups.push({
      type: 'group',
      label: '管理',
      children: [{ key: '/admin', icon: icon(<ShieldCheck size={16} />), label: '系统' }],
    })
  }
  return groups
}

/** 当前选中项：取 pathname 最长前缀匹配的顶级段 */
function selectedKey(pathname: string): string[] {
  const top = '/' + pathname.split('/')[1]
  if (pathname.startsWith('/settings/') || pathname.startsWith('/admin')) return [pathname]
  return [top]
}

export function Sidebar({
  collapsed = false,
  onToggle,
  onNavigate,
}: {
  collapsed?: boolean
  onToggle?: () => void
  /** 导航后回调：紧凑屏抽屉形态下点击菜单项即关抽屉 */
  onNavigate?: () => void
}): ReactNode {
  const navigate = useNavigate()
  const location = useLocation()
  const { kb_enabled } = useCapabilities()
  const isManager = useCurrentUser((s) => s.user?.is_manager ?? false)

  // 知识库菜单项随能力增减；未就绪时 kb_enabled=false → 不渲染（避免点到 404）
  const items = useMemo(() => buildItems(kb_enabled, isManager), [kb_enabled, isManager])

  const onClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key)
    onNavigate?.()
  }

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* 品牌带 76px：展开态 logo 底对齐居中（原位不动），折叠钮绝对定位于右缘，
          与 logo 同处一条视觉带；折叠态 logo 缩小、折叠钮移至其正下方居中。
          高度计入布局而非 margin 溢出——曾用 marginTop 补偿导致卡片滚动条 */}
      <div
        style={{
          height: 76,
          display: 'flex',
          flexDirection: collapsed ? 'column' : 'row',
          alignItems: collapsed ? 'center' : 'flex-end',
          justifyContent: 'center',
          gap: collapsed ? 4 : 0,
          padding: collapsed ? '6px 0' : '0 20px',
          fontWeight: 700,
          fontSize: 18,
          flexShrink: 0,
          position: 'relative',
        }}
      >
        <img
          src="/logo-transparent.png"
          alt="goduck"
          style={{
            maxHeight: collapsed ? 32 : 56,
            maxWidth: '100%',
            objectFit: 'contain',
            transition: 'max-height .2s ease',
          }}
        />
        {/* 折叠钮仅常驻侧栏形态渲染（onToggle 由 AppShell 传入）；抽屉形态
            （onToggle 缺省）不渲染——抽屉有自身的关闭路径（遮罩/导航即关） */}
        {onToggle ? (
          <button
            type="button"
            className="sider-toggle"
            onClick={onToggle}
            title={collapsed ? '展开侧边栏' : '收起侧边栏'}
            aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
            aria-expanded={!collapsed}
            style={collapsed ? undefined : { position: 'absolute', right: 12, bottom: 11 }}
          >
            {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={16} />}
          </button>
        ) : null}
      </div>
      {/* 菜单区自身滚动：菜单项再多也不撑出卡片级滚动条（卡片整体不滚动）。
          inlineCollapsed 随折叠钮联动：折叠后图标化 + Tooltip，分组标签隐去 */}
      <Menu
        mode="inline"
        inlineCollapsed={collapsed}
        selectedKeys={selectedKey(location.pathname)}
        items={items}
        onClick={onClick}
        style={{ flex: 1, borderInlineEnd: 'none', overflowX: 'hidden', overflowY: 'auto', paddingBottom: 8 }}
      />
      {/* 底部工具区（菜单下方）：主题切换（Light|Dark 分段胶囊）+ 用户芯片；
           折叠时两者都退化为居中圆钮。与菜单间以留白区隔（无分割线） */}
      <div
        style={{
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 8,
          padding: collapsed ? '10px 0 12px' : '10px 12px 12px',
        }}
      >
        <ThemeToggle collapsed={collapsed} />
        <UserMenu collapsed={collapsed} />
      </div>
    </div>
  )
}
