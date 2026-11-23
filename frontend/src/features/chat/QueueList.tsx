/* 队列条带（queued-send §6）—— 消息流末尾的待发队列视图：
    - paused 横幅（error/withdrawn/restart 原因）+ [继续][清空]；
    - 行卡片：状态左强调条 + 序号/状态徽标，内容两行截断，
      附件/知识库收敛为 chip，操作钮恒居卡片右侧（垂直居中）；
    - FIFO 顺序由序号徽标隐式表达，不占文案；
    - failed 行展示复检失败原因 + [回填草稿] 拯救；
    - canceled 拯救列表：[重新排队]（单条/全部）+ [回填草稿] + 删除。
    真值以 GET /queue 为准（终态回查驱动），本地操作即时回显。 */
import { Alert, App as AntApp, Button, Flex, Tooltip } from 'antd'
import { type ReactNode, useState } from 'react'
import { Ban, BookOpen, Clock, CornerUpLeft, Loader2, Paperclip, RotateCcw, Trash2, XCircle } from 'lucide-react'
import { queueApi } from '@/api/queue'
import { useChatStore } from '@/store/chatStore'
import { watchQueueDrain } from '@/hooks/useStreamChat'
import { toastError } from '@/lib/antdStatic'
import type { QueuedItem, UUID } from '@/types'

const PAUSE_REASON_TEXT: Record<string, string> = {
  error: '上一回合出错，队列已暂停',
  withdrawn: '已回撤历史回合，队列已暂停',
  restart: '服务重启，队列已挂起',
}

function QueueRow({ item, sessionId, seq }: { item: QueuedItem; sessionId: UUID; seq?: number }): ReactNode {
  const { modal } = AntApp.useApp()
  const removeQueueItem = useChatStore((s) => s.removeQueueItem)
  const setDraft = useChatStore((s) => s.setDraft)
  const setAttachments = useChatStore((s) => s.setAttachments)
  const setKbs = useChatStore((s) => s.setKbs)
  const [busy, setBusy] = useState(false)

  const run = async (fn: () => Promise<void>) => {
    if (busy) return
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      toastError(e instanceof Error ? e.message : '操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const backfill = () => {
    setDraft(item.content)
    setAttachments(item.attachments ?? [])
    setKbs(item.kbs ?? [])
  }

  const del = () =>
    run(async () => {
      await queueApi.deleteItem(sessionId, item.id)
      removeQueueItem(item.id)
    })

  const requeue = () =>
    run(async () => {
      const s = useChatStore.getState()
      await queueApi.requeue(sessionId, [item.id])
      s.syncQueue(await queueApi.getQueue(sessionId))
      watchQueueDrain(sessionId)
    })

  // 左缘徽标：pending=FIFO 序号 | sending=旋转指示 | failed/canceled=状态图标
  const badge: ReactNode =
    item.status === 'sending' ? (
      <Loader2 size={13} className="queue-strip__spin" />
    ) : item.status === 'failed' ? (
      <XCircle size={13} />
    ) : item.status === 'canceled' ? (
      <Ban size={13} />
    ) : (
      seq
    )

  const meta: ReactNode[] = []
  if (item.status === 'sending') {
    meta.push(
      <span key="sending" className="queue-strip__chip queue-strip__chip--live">
        发送中
      </span>,
    )
  }
  if (item.status === 'canceled') {
    meta.push(
      <span key="canceled" className="queue-strip__chip">
        已取消
      </span>,
    )
  }
  if (item.status === 'failed') {
    meta.push(
      <span key="reason" className="queue-strip__reason" title={item.fail_reason ?? undefined}>
        失败{item.fail_reason ? `：${item.fail_reason}` : ''}
      </span>,
    )
  }
  if (item.attachments.length) {
    meta.push(
      <span key="att" className="queue-strip__chip">
        <Paperclip size={11} />
        {item.attachments.length} 个附件
      </span>,
    )
  }
  if (item.kbs.length) {
    const kbNames = item.kbs.map((k) => k.name).join('、')
    meta.push(
      <span key="kb" className="queue-strip__chip queue-strip__chip--kb" title={kbNames}>
        <BookOpen size={11} />
        {kbNames}
      </span>,
    )
  }

  return (
    <Flex gap={8} className="queue-strip__row" data-status={item.status}>
      <span className="queue-strip__badge">{badge}</span>
      <div className="queue-strip__body">
        <div className="queue-strip__content" title={item.content}>
          {item.content}
        </div>
        {meta.length ? <div className="queue-strip__meta">{meta}</div> : null}
      </div>
      <Flex gap={2} align="center" className="queue-strip__actions">
        {item.status === 'canceled' || item.status === 'failed' ? (
          <>
            <Tooltip title="重新排队（保留原排队顺序）">
              <Button type="text" size="small" icon={<RotateCcw size={13} />} loading={busy} onClick={requeue} />
            </Tooltip>
            <Tooltip title="回填到输入框编辑后重发">
              <Button type="text" size="small" icon={<CornerUpLeft size={13} />} onClick={backfill} />
            </Tooltip>
          </>
        ) : null}
        {item.status !== 'sending' ? (
          <Tooltip title="删除">
            <Button
              type="text"
              size="small"
              danger
              icon={<Trash2 size={13} />}
              onClick={() =>
                modal.confirm({
                  title: '删除该排队消息？',
                  okType: 'danger',
                  okText: '删除',
                  cancelText: '取消',
                  onOk: del,
                })
              }
            />
          </Tooltip>
        ) : null}
      </Flex>
    </Flex>
  )
}

export function QueueList({ sessionId }: { sessionId: UUID }): ReactNode {
  const items = useChatStore((s) => s.queueItems)
  const queueState = useChatStore((s) => s.queueState)
  const pausedReason = useChatStore((s) => s.queuePausedReason)
  const syncQueue = useChatStore((s) => s.syncQueue)
  const [busy, setBusy] = useState(false)

  // 无条目即不占位：排水进行中（active）队头已物化为时间线气泡，空条带不留框
  if (!items.length && queueState !== 'paused') return null

  const run = async (fn: () => Promise<unknown>) => {
    if (busy) return
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      toastError(e instanceof Error ? e.message : '操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  const pending = items.filter((q) => q.status === 'pending' || q.status === 'sending')
  const salvage = items.filter((q) => q.status === 'canceled' || q.status === 'failed')

  return (
    <Flex vertical gap={6} className="queue-strip">
      {queueState === 'paused' ? (
        <Alert
          type="warning"
          showIcon
          message={PAUSE_REASON_TEXT[pausedReason ?? ''] ?? '队列已暂停'}
          action={
            <Flex gap={4}>
              <Button
                size="small"
                loading={busy}
                onClick={() =>
                  run(async () => {
                    syncQueue(await queueApi.continueQueue(sessionId))
                    watchQueueDrain(sessionId)
                  })
                }
              >
                继续
              </Button>
              <Button
                size="small"
                danger
                loading={busy}
                onClick={() =>
                  run(async () => {
                    await queueApi.clearQueue(sessionId)
                    syncQueue(await queueApi.getQueue(sessionId))
                  })
                }
              >
                清空
              </Button>
            </Flex>
          }
        />
      ) : null}
      {pending.length ? (
        <div className="queue-strip__group">
          <Flex gap={6} align="center" className="queue-strip__header">
            <Clock size={12} className="queue-strip__header-icon" />
            <span className="queue-strip__title">待发队列</span>
            <span className="queue-strip__count">{pending.length} 条</span>
          </Flex>
          {pending.map((item, i) => (
            <QueueRow key={item.id} item={item} sessionId={sessionId} seq={i + 1} />
          ))}
        </div>
      ) : null}
      {salvage.length ? (
        <div className="queue-strip__group">
          <Flex gap={6} align="center" className="queue-strip__header">
            <span className="queue-strip__title">已取消 / 失败</span>
            <span className="queue-strip__count">{salvage.length} 条</span>
            <Button
              size="small"
              type="link"
              icon={<RotateCcw size={12} />}
              loading={busy}
              onClick={() =>
                run(async () => {
                  await queueApi.requeue(sessionId)
                  syncQueue(await queueApi.getQueue(sessionId))
                  watchQueueDrain(sessionId)
                })
              }
            >
              全部重新排队
            </Button>
          </Flex>
          {salvage.map((item) => (
            <QueueRow key={item.id} item={item} sessionId={sessionId} />
          ))}
        </div>
      ) : null}
    </Flex>
  )
}
