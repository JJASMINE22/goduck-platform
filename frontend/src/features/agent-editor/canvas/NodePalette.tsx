/* 模块面板（左侧）—— 子代理模板，拖出到画布即建节点。
   draggable 携带模板 key；画布 onDrop 读取并 addNode。点击空白模板同样建节点（挂选中/根下）。 */
import { Flex, Typography } from 'antd'
import { Boxes, Plus } from 'lucide-react'
import { type ReactNode } from 'react'
import { useAgentEditorStore } from '@/store/agentEditorStore'
import { SUBAGENT_TEMPLATES } from '../templates'

const TPL_MIME = 'application/subagent-template'

export function NodePalette(): ReactNode {
  const addNode = useAgentEditorStore((s) => s.addNode)
  const selectedNodeId = useAgentEditorStore((s) => s.selectedNodeId)
  const nodes = useAgentEditorStore((s) => s.nodes)
  const rootId = nodes?.find((n) => n.parentId === null)?.id ?? null

  const onDragStart = (e: React.DragEvent, key: string) => {
    e.dataTransfer.setData(TPL_MIME, key)
    e.dataTransfer.effectAllowed = 'move'
  }

  // 点击即建（无需拖拽）：默认挂选中节点，否则挂根
  const onClickAdd = (key: string) => {
    const parentId = selectedNodeId ?? rootId
    if (!parentId) return
    const tpl = SUBAGENT_TEMPLATES.find((t) => t.key === key)
    addNode(parentId, tpl?.fields)
  }

  return (
    <div className="node-palette">
      <Typography.Text strong style={{ fontSize: 13, padding: '0 4px' }}>
        模块
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 11, padding: '0 4px 8px', display: 'block' }}>
        拖到画布或点击添加
      </Typography.Text>
      <Flex vertical gap={8}>
        {SUBAGENT_TEMPLATES.map((t) => (
          <div
            key={t.key}
            className="palette-item"
            draggable
            onDragStart={(e) => onDragStart(e, t.key)}
            onClick={() => onClickAdd(t.key)}
            title={t.hint}
          >
            {t.key === 'blank' ? <Plus size={15} /> : <Boxes size={15} />}
            <span className="palette-item__label">
              <span className="palette-item__name">{t.label}</span>
              <small className="palette-item__hint">{t.hint}</small>
            </span>
          </div>
        ))}
      </Flex>
    </div>
  )
}
