/* HITL 决策卡 —— interrupt 事件到达后渲染。对应后端 schemas.Decision。

   deepagents 契约：模型一轮里可能产出 N 个需审批的工具调用，全部打包进同
   一个 interrupt（payload.action_requests 长度为 N）。resume 时必须按顺序给出
   N 个 decision（与 action_requests 1:1），否则报
   "Number of human decisions (X) does not match number of hanging tool calls (Y)"。

   因此本卡逐条渲染每个待审批工具调用，各自独立审核（approve/edit/reject/respond），
   收齐全部 N 条决策后才整体 resume。allowed_decisions（来自 review_configs[i]）
   用于动态启用/禁用对应按钮，尊重服务端配置。 */
import { Button, Flex, Input, Space, Tag, Typography } from 'antd'
import { type ReactNode, useMemo, useState } from 'react'
import { Check, Edit3, MessageCircleReply, X } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type {
  ActionRequest,
  Decision,
  DecisionType,
  InterruptPayload,
  ReviewConfig,
} from '@/types'
import { toastError } from '@/lib/antdStatic'

const ALL_DECISIONS: DecisionType[] = ['approve', 'edit', 'reject', 'respond']

/** 已确认决策的摘要文案 / 标签色 */
function committedLabel(t: DecisionType): string {
  switch (t) {
    case 'approve':
      return '已通过'
    case 'edit':
      return '已编辑'
    case 'reject':
      return '已拒绝'
    case 'respond':
      return '已回复'
  }
}

function committedTagColor(t: DecisionType): string {
  switch (t) {
    case 'approve':
      return 'green'
    case 'edit':
      return 'geekblue'
    case 'reject':
      return 'red'
    case 'respond':
      return 'purple'
  }
}

function initDecisions(payload: InterruptPayload | undefined): (Decision | null)[] {
  const n = payload?.action_requests?.length ?? 0
  return Array.from({ length: n }, () => null)
}

/** 单个 action_request 的决策卡：选模式→（如需）填参/留言→确认。
 *  onCommit 上报该条决策（null 表示撤销重选）；父组件收齐 N 条后整体 resume。 */
function ActionDecisionCard({
  index,
  action,
  review,
  committed,
  onCommit,
}: {
  index: number
  action: ActionRequest
  review: ReviewConfig
  committed: Decision | null
  onCommit: (d: Decision | null) => void
}): ReactNode {
  const allowed = useMemo<DecisionType[]>(
    () => (review.allowed_decisions?.length ? review.allowed_decisions : ALL_DECISIONS),
    [review],
  )
  const name = action.name ?? '未知工具'
  const args = action.args ?? {}

  const [mode, setMode] = useState<DecisionType | null>(null)
  const [message, setMessage] = useState('')
  const [argsText, setArgsText] = useState('')

  const resetTransient = () => {
    setMode(null)
    setMessage('')
    setArgsText('')
  }

  // 已确认 → 仅显示摘要 + 重新决策入口（重选会把已提交的该条决策清掉）
  if (committed) {
    return (
      <Flex align="center" justify="space-between" gap={8} style={{ marginTop: 10 }}>
        <Space size={6} wrap>
          <Tag color="blue">
            #{index + 1} {name}
          </Tag>
          <Tag color={committedTagColor(committed.type)}>{committedLabel(committed.type)}</Tag>
        </Space>
        <Button size="small" onClick={() => onCommit(null)}>
          修改
        </Button>
      </Flex>
    )
  }

  const confirm = () => {
    let decision: Decision
    if (mode === 'edit') {
      let edited: Record<string, unknown>
      try {
        edited = argsText.trim() ? (JSON.parse(argsText) as Record<string, unknown>) : args
      } catch {
        toastError(`第 ${index + 1} 项的参数不是合法 JSON`)
        return
      }
      decision = { type: 'edit', edited_action: { name, args: edited } }
    } else if (mode === 'reject' || mode === 'respond') {
      decision = { type: mode, message: message || null }
    } else {
      // 兜底：走到 confirm 时 mode 必为需要额外输入的三者之一；approve 不经此处
      decision = { type: mode as DecisionType }
    }
    resetTransient()
    onCommit(decision)
  }

  return (
    <div
      style={{
        marginTop: 10,
        paddingTop: 10,
        borderTop: '1px dashed var(--color-border-soft)',
      }}
    >
      <Flex align="center" gap={8} style={{ marginBottom: 8 }}>
        <Tag color="blue">#{index + 1}</Tag>
        <Typography.Text strong>
          工具调用待审批：<code>{name}</code>
        </Typography.Text>
      </Flex>
      <pre
        style={{
          margin: '0 0 12px',
          background: 'var(--color-border-soft)',
          padding: 8,
          borderRadius: 6,
          fontSize: 12,
          overflow: 'auto',
          maxHeight: 200,
        }}
      >
        {JSON.stringify(args, null, 2)}
      </pre>

      {mode === 'edit' ? (
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            编辑后的参数（JSON）
          </Typography.Text>
          <Input.TextArea
            rows={5}
            value={argsText}
            onChange={(e) => setArgsText(e.target.value)}
            placeholder={JSON.stringify(args, null, 2)}
            style={{ fontFamily: 'var(--font-mono)', fontSize: 12, marginTop: 4 }}
          />
        </div>
      ) : null}

      {mode === 'reject' || mode === 'respond' ? (
        <div style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {mode === 'reject' ? '拒绝说明（可选）' : '回复内容'}
          </Typography.Text>
          <Input.TextArea
            rows={2}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            style={{ marginTop: 4 }}
          />
        </div>
      ) : null}

      <Space wrap>
        {mode === null ? (
          <>
            {allowed.includes('approve') ? (
              <Button type="primary" icon={<Check size={14} />} onClick={() => onCommit({ type: 'approve' })}>
                通过
              </Button>
            ) : null}
            {allowed.includes('edit') ? (
              <Button icon={<Edit3 size={14} />} onClick={() => setMode('edit')}>
                编辑后通过
              </Button>
            ) : null}
            {allowed.includes('respond') ? (
              <Button icon={<MessageCircleReply size={14} />} onClick={() => setMode('respond')}>
                直接回复
              </Button>
            ) : null}
            {allowed.includes('reject') ? (
              <Button danger icon={<X size={14} />} onClick={() => setMode('reject')}>
                拒绝
              </Button>
            ) : null}
          </>
        ) : (
          <>
            <Button
              type="primary"
              onClick={confirm}
            >
              确认{mode === 'edit' ? '编辑' : mode === 'reject' ? '拒绝' : '回复'}
            </Button>
            <Button onClick={resetTransient}>取消</Button>
          </>
        )}
      </Space>
    </div>
  )
}

export function HitlCard({
  resume,
}: {
  resume: (decisions: Decision[]) => Promise<void>
}): ReactNode {
  const pending = useChatStore((s) => s.pendingInterrupt)
  const [decisions, setDecisions] = useState<(Decision | null)[]>(() =>
    initDecisions(pending?.payload),
  )
  // 新一轮 interrupt 到达（arrivedAt 变更）→ 重置决策集合，避免上一轮残留。
  // 采用 React 推荐的"渲染期根据前置值调整 state"模式（非 effect，不触发级联渲染）。
  const arrivedAt = pending?.arrivedAt ?? 0
  const [prevArrivedAt, setPrevArrivedAt] = useState(arrivedAt)
  if (arrivedAt !== prevArrivedAt) {
    setPrevArrivedAt(arrivedAt)
    setDecisions(initDecisions(pending?.payload))
  }

  if (!pending) return null
  const payload: InterruptPayload = pending.payload
  const actions: ActionRequest[] = payload.action_requests ?? []

  // 防御：极端情况下长度不一致时以 actions 长度为准，对齐显示（不做渲染期 setState）
  const safeDecisions =
    decisions.length === actions.length ? decisions : actions.map(() => null)
  const committedCount = safeDecisions.filter(Boolean).length
  const allCommitted = actions.length > 0 && committedCount === actions.length

  const setAt = (i: number, d: Decision | null) =>
    setDecisions((prev) => {
      const base = prev.length === actions.length ? prev : actions.map(() => null)
      const next = [...base]
      next[i] = d
      return next
    })

  const submit = () => {
    const out = safeDecisions.filter(Boolean) as Decision[]
    if (out.length !== actions.length) {
      toastError(`需要为全部 ${actions.length} 个工具调用给出决策（当前 ${out.length}）`)
      return
    }
    void resume(out)
  }

  return (
    <Flex justify="flex-start">
      <div className="hitl-card" style={{ width: '78%', padding: 14 }}>
        <Flex align="center" gap={8} style={{ marginBottom: 4 }}>
          <Tag color="orange">人工审批</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            共 {actions.length} 个工具调用待审批（需逐条决策后统一提交）
          </Typography.Text>
        </Flex>

        {actions.map((action, i) => (
          <ActionDecisionCard
            key={i}
            index={i}
            action={action}
            review={(payload.review_configs?.[i] as ReviewConfig) ?? {}}
            committed={safeDecisions[i] ?? null}
            onCommit={(d) => setAt(i, d)}
          />
        ))}

        <Flex justify="flex-end" style={{ marginTop: 12 }}>
          <Button type="primary" disabled={!allCommitted} onClick={submit}>
            提交全部决策（{committedCount}/{actions.length}）
          </Button>
        </Flex>
      </div>
    </Flex>
  )
}
