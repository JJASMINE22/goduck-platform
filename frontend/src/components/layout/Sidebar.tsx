/* 侧边导航 —— 对应 DESIGN §9 路由结构。AntD Menu + react-router 导航。
   能力驱动（F2）：知识库菜单仅在后端 kb_enabled=true 时出现（单一真相源 /health）。 */
import { Bot, BookOpen, Cpu, Database, MessagesSquare, Network } from 'lucide-react'
import { Menu } from 'antd'
import type { MenuProps } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMemo, type ReactNode } from 'react'
import { useCapabilities } from '@/hooks/useCapabilities'

type MenuItem = Required<MenuProps>['items'][number]

const icon = (n: ReactNode) => <span style={{ display: 'inline-flex', verticalAlign: 'middle' }}>{n}</span>

/** 全量菜单项；知识库项按能力动态过滤 */
function buildItems(kbEnabled: boolean): MenuItem[] {
  const settings: MenuItem[] = [
    { key: '/settings/models', icon: icon(<Cpu size={16} />), label: '模型' },
    { key: '/settings/mcp', icon: icon(<Network size={16} />), label: 'MCP 服务' },
    { key: '/settings/skills', icon: icon(<BookOpen size={16} />), label: '技能' },
  ]
  if (kbEnabled) {
    settings.push({ key: '/settings/knowledge', icon: icon(<Database size={16} />), label: '知识库' })
  }
  return [
    { key: '/agents', icon: icon(<Bot size={16} />), label: '智能体' },
    { key: '/sessions', icon: icon(<MessagesSquare size={16} />), label: '会话' },
    { type: 'group', label: '设置', children: settings },
  ]
}

/** 当前选中项：取 pathname 最长前缀匹配的顶级段 */
function selectedKey(pathname: string): string[] {
  const top = '/' + pathname.split('/')[1]
  if (pathname.startsWith('/settings/')) return [pathname]
  return [top]
}

export function Sidebar({ collapsed = false }: { collapsed?: boolean }): ReactNode {
  const navigate = useNavigate()
  const location = useLocation()
  const { kb_enabled } = useCapabilities()

  // 知识库菜单项随能力增减；未就绪时 kb_enabled=false → 不渲染（避免点到 404）
  const items = useMemo(() => buildItems(kb_enabled), [kb_enabled])

  const onClick: MenuProps['onClick'] = ({ key }) => navigate(key)

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          height: 56,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: collapsed ? '0 12px' : '0 20px',
          fontWeight: 700,
          fontSize: 18,
          borderBottom: '1px solid var(--color-border-soft)',
        }}
      >
        <img
          src="/logo-transparent.png"
          alt="goduck"
          style={{ maxHeight: '100%', maxWidth: '100%', marginTop: 20 }}
        />
      </div>
      <Menu
        mode="inline"
        selectedKeys={selectedKey(location.pathname)}
        defaultOpenKeys={['设置组']}
        items={items}
        onClick={onClick}
        style={{ flex: 1, borderInlineEnd: 'none' }}
      />
    </div>
  )
}
