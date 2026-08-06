/* 智能体拓扑悬浮预览（DESIGN §10.2）—— 列表行名称上悬浮，按需拉取 topology.mmd。
   仅在 Popover 打开时启用查询（enabled），避免列表渲染即 N+1；同一 agent 的拓扑缓存复用。
   loading / 空拓扑 / 错误各有占位。 */
import { Popover, Spin, Typography } from 'antd'
import { type ReactNode, useState } from 'react'
import { useAgentTopology } from '@/hooks/useAgents'
import { MermaidPreview } from './MermaidPreview'
import type { UUID } from '@/types'

export function AgentTopologyHover({ id, children }: { id: UUID; children: ReactNode }): ReactNode {
  const [open, setOpen] = useState(false)
  const { data, isLoading, isError } = useAgentTopology(id, open)

  return (
    <Popover
      trigger="hover"
      placement="rightTop"
      open={open}
      onOpenChange={setOpen}
      mouseEnterDelay={0.25}
      styles={{ body: { width: 440, maxHeight: 440, overflow: 'auto' } }}
      title="智能体拓扑"
      content={
        isError ? (
          <Typography.Text type="danger" style={{ fontSize: 12 }}>加载失败，请重试</Typography.Text>
        ) : isLoading ? (
          <Flex><Spin size="small" /></Flex>
        ) : (
          <MermaidPreview code={data ?? ''} />
        )
      }
    >
      {children}
    </Popover>
  )
}

function Flex({ children }: { children: ReactNode }): ReactNode {
  return <div style={{ display: 'flex', justifyContent: 'center', padding: 12 }}>{children}</div>
}
