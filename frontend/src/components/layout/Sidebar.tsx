/* 侧边导航 —— 对应 DESIGN §9 路由结构。AntD Menu + react-router 导航。 */
import { Bot, BookOpen, Cpu, MessagesSquare, Network } from 'lucide-react'
import { Menu } from 'antd'
import type { MenuProps } from 'antd'
import { useLocation, useNavigate } from 'react-router-dom'
import type { ReactNode } from 'react'

type MenuItem = Required<MenuProps>['items'][number]

const icon = (n: ReactNode) => <span style={{ display: 'inline-flex', verticalAlign: 'middle' }}>{n}</span>

const items: MenuItem[] = [
  { key: '/agents', icon: icon(<Bot size={16} />), label: '智能体' },
  { key: '/sessions', icon: icon(<MessagesSquare size={16} />), label: '会话' },
  {
    type: 'group',
    label: '设置',
    children: [
      { key: '/settings/models', icon: icon(<Cpu size={16} />), label: '模型' },
      { key: '/settings/mcp', icon: icon(<Network size={16} />), label: 'MCP 服务' },
      { key: '/settings/skills', icon: icon(<BookOpen size={16} />), label: '技能' },
    ],
  },
]

/** 当前选中项：取 pathname 最长前缀匹配的顶级段 */
function selectedKey(pathname: string): string[] {
  const top = '/' + pathname.split('/')[1]
  if (pathname.startsWith('/settings/')) return [pathname]
  return [top]
}

export function Sidebar(): ReactNode {
  const navigate = useNavigate()
  const location = useLocation()

  const onClick: MenuProps['onClick'] = ({ key }) => navigate(key)

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div
        style={{
          height: 56,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '0 20px',
          fontWeight: 700,
          fontSize: 16,
          borderBottom: '1px solid var(--color-border-soft)',
        }}
      >
        <span style={{ color: 'var(--color-primary)' }}>🦆</span>
        goduck
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
