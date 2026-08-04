/* 已选工具的分组折叠展示 —— 替代 TreeSelect 框内扁平 tag 堆叠。
   按 {server}__{tool} 的 server 前缀分组，每组一个 Collapse 面板：
   标题 = server 名 + 已选数量徽章；展开 = 工具短名 + 描述（可移除）。
   默认展开以便立即查看所选，嫌长可手动收起。 */
import { Collapse, Flex, Tag } from 'antd'
import { type ReactNode } from 'react'
import { X } from 'lucide-react'
import type { ToolInfo } from '@/types'

function splitServer(fullName: string): { server: string; short: string } {
  const idx = fullName.indexOf('__')
  if (idx > 0) return { server: fullName.slice(0, idx), short: fullName.slice(idx + 2) }
  return { server: '其他', short: fullName }
}

export function CheckedToolGroups({
  selected,
  tools,
  onRemove,
}: {
  selected: string[]
  tools: ToolInfo[]
  onRemove: (fullName: string) => void
}): ReactNode {
  if (!selected.length) return null

  const byServer = new Map<string, string[]>()
  for (const name of selected) {
    const { server } = splitServer(name)
    const arr = byServer.get(server) ?? []
    arr.push(name)
    byServer.set(server, arr)
  }
  const descMap = new Map(tools.map((t) => [t.name, t.description]))
  const servers = Array.from(byServer.keys())

  const items = servers.map((server) => {
    const names = byServer.get(server)!
    return {
      key: server,
      label: (
        <Flex align="center" gap={6}>
          <span>{server}</span>
          <Tag style={{ marginInlineEnd: 0 }}>{names.length}</Tag>
        </Flex>
      ),
      children: (
        <Flex vertical gap={2}>
          {names.map((fullName) => {
            const { short } = splitServer(fullName)
            const desc = descMap.get(fullName)
            return (
              <Flex key={fullName} align="center" gap={8} className="checked-tool">
                <code className="checked-tool__name" title={fullName}>
                  {short}
                </code>
                {desc ? (
                  <span className="checked-tool__desc" title={desc}>
                    {desc}
                  </span>
                ) : null}
                <button
                  type="button"
                  className="checked-tool__remove"
                  title="移除该工具"
                  onClick={() => onRemove(fullName)}
                >
                  <X size={12} />
                </button>
              </Flex>
            )
          })}
        </Flex>
      ),
    }
  })

  return (
    <Collapse
      size="small"
      className="checked-tool-groups"
      defaultActiveKey={servers}
      items={items}
    />
  )
}
