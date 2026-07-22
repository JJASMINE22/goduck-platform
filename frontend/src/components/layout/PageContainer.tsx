/* 通用页面容器 —— 统一「标题 + 操作区 + 内容」布局，全工作台复用。 */
import { Flex, Typography } from 'antd'
import type { ReactNode } from 'react'

export function PageContainer({
  title,
  subtitle,
  extra,
  children,
}: {
  title: string
  subtitle?: string
  extra?: ReactNode
  children: ReactNode
}): ReactNode {
  return (
    <Flex vertical gap={16} style={{ height: '100%' }}>
      <Flex justify="space-between" align="flex-end" wrap gap={12}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {title}
          </Typography.Title>
          {subtitle ? (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              {subtitle}
            </Typography.Text>
          ) : null}
        </div>
        {extra ? <Flex gap={8}>{extra}</Flex> : null}
      </Flex>
      <div style={{ flex: 1, minHeight: 0 }}>{children}</div>
    </Flex>
  )
}
