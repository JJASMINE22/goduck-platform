/* 会话显示密度切换（简洁 | 详细）—— composer 工具条入口：图标钮 + Dropdown
    单选列表。选中项 = 文本后置裸 ✔（主色，无 Checkbox 框体）；Tooltip 与浮层
    的互斥处理同知识库选择器。图标随模式切换（Eye=详细 / EyeOff=简洁），
    状态即所见，无需文字标注。
    组件自订阅 chatDisplayStore：模式变化只重渲染本切换器与 MessageList
    （订阅方各取所需），ChatInput / ChatPage 不感知。语义见 chatDisplayStore 文件头。
    显示偏好与发送动作无关：任何会话状态（含流式生成中）均可即时切换。 */
import { Button, Dropdown, Tooltip } from 'antd'
import { type ReactNode, useState } from 'react'
import { Check, Eye, EyeOff } from 'lucide-react'
import { useChatDisplayStore, type ChatDisplayMode } from '@/store/chatDisplayStore'

const OPTIONS: Array<{ value: ChatDisplayMode; label: string }> = [
  { value: 'detailed', label: '详细' },
  { value: 'concise', label: '简洁' },
]

export function DisplayModeToggle(): ReactNode {
  const mode = useChatDisplayStore((s) => s.mode)
  const setMode = useChatDisplayStore((s) => s.setMode)
  const [open, setOpen] = useState(false)
  const concise = mode === 'concise'

  return (
    <Dropdown
      trigger={['click']}
      placement="topLeft"
      open={open}
      onOpenChange={setOpen}
      menu={{
        // 不用 menu 的 selectable 选中蒙版：自渲染裸 ✔（文本后置、主色），
        // 选中态视觉更直接（见 .display-mode-item 样式）
        items: OPTIONS.map((o) => ({
          key: o.value,
          label: (
            <span className="display-mode-item">
              {o.label}
              {mode === o.value ? <Check size={14} className="display-mode-item__check" /> : null}
            </span>
          ),
        })),
        onClick: ({ key }) => {
          setMode(key as ChatDisplayMode)
          setOpen(false)
        },
      }}
    >
      <Tooltip title="简洁 / 详细" placement="top" open={open ? false : undefined}>
        <Button
          type="text"
          shape="circle"
          size="middle"
          icon={concise ? <EyeOff size={16} /> : <Eye size={16} />}
          aria-label="简洁 / 详细"
        />
      </Tooltip>
    </Dropdown>
  )
}
