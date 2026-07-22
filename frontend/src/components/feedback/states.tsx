/* 通用反馈组件：加载 / 空态 / 错误态。统一工作台视觉。 */
import { Button, Empty, Result, Spin } from 'antd'
import { Inbox } from 'lucide-react'
import { type ReactNode } from 'react'

export function PageLoading({ tip = '加载中…' }: { tip?: string }): ReactNode {
  return (
    <div style={{ display: 'grid', placeItems: 'center', padding: '64px 0' }}>
      <Spin tip={tip} size="large" />
    </div>
  )
}

export function EmptyState({
  title = '暂无数据',
  description,
  icon,
  extra,
}: {
  title?: string
  description?: string
  icon?: ReactNode
  extra?: ReactNode
}): ReactNode {
  return (
    <Empty
      image={icon ?? <Inbox size={48} color="var(--color-text-tertiary)" />}
      description={
        <span>
          {title}
          {description ? (
            <small style={{ display: 'block', color: 'var(--color-text-tertiary)' }}>
              {description}
            </small>
          ) : null}
        </span>
      }
      style={{ padding: '48px 0' }}
    >
      {extra}
    </Empty>
  )
}

export function ErrorState({
  message,
  onRetry,
}: {
  message?: string
  onRetry?: () => void
}): ReactNode {
  return (
    <Result
      status="warning"
      title="加载失败"
      subTitle={message ?? '请检查后端服务是否正常运行'}
      extra={onRetry ? <Button onClick={onRetry}>重试</Button> : undefined}
    />
  )
}
