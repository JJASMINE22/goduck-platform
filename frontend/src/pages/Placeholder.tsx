/* 占位组件 —— 骨架阶段各路由暂用，逐域实现时替换对应页面文件内容。 */
import { Card, Typography } from 'antd'
import { Hammer } from 'lucide-react'
import type { ReactNode } from 'react'

export function Placeholder({ title }: { title: string }): ReactNode {
  return (
    <Card>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        {title}
      </Typography.Title>
      <Typography.Text type="secondary">
        <Hammer size={14} style={{ verticalAlign: '-2px', marginRight: 6 }} />
        该模块建设中
      </Typography.Text>
    </Card>
  )
}
