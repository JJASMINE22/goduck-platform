/* 占位组件 —— 骨架阶段各路由暂用，逐域实现时替换对应页面文件内容。 */
import { Typography } from 'antd'
import { Hammer } from 'lucide-react'
import type { ReactNode } from 'react'

export function Placeholder({ title }: { title: string }): ReactNode {
  return (
    <div className="page-body" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        {title}
      </Typography.Title>
      <Typography.Text type="secondary">
        <Hammer size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />
        该模块建设中
      </Typography.Text>
    </div>
  )
}
