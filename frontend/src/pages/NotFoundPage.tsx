import { Button, Result } from 'antd'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

export function NotFoundPage(): ReactNode {
  const navigate = useNavigate()
  return (
    <Result
      status="404"
      title="404"
      subTitle="页面不存在"
      extra={
        <Button type="primary" onClick={() => navigate('/agents')}>
          返回智能体列表
        </Button>
      }
    />
  )
}
