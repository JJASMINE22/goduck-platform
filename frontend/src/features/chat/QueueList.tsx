/* 队列条带（queued-send §6）—— 消息流末尾的待发队列视图：
   - paused 横幅（error/withdrawn/restart 原因）+ [继续][清空]；
   - pending 半透明排队气泡（FIFO 序，可删除）；sending 队首 = 正在发送；
   - failed 行展示复检失败原因 + [回填草稿] 拯救；
   - canceled 拯救列表：[重新排队]（单条/全部）+ [回填草稿] + 删除。
   真值以 GET /queue 为准（终态回查驱动），本地操作即时回显。 */
import { Alert, App as AntApp, Button, Flex, Tooltip, Typography } from 'antd'
import { type ReactNode, useState } from 'react'
import { Clock, CornerUpLeft, RotateCcw, Trash2 } from 'lucide-react'
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

function QueueRow({ item, sessionId }: { item: QueuedItem; sessionId: UUID }): ReactNode {
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

  const statusTag =
    item.status === 'sending' ? (
      <Typography.Text type="secondary">发送中…</Typography.Text>
    ) : item.status === 'failed' ? (
      <Typography.Text type="danger" title={item.fail_reason ?? undefined}>
        失败{item.fail_reason ? `：${item.fail_reason}` : ''}
      </Typography.Text>
    ) : item.status === 'canceled' ? (
      <Typography.Text type="secondary">已取消</Typography.Text>
    ) : null

  return (
    <Flex
      vertical
      gap={2}
      className="queue-strip__row"
      data-status={item.status}
      style={{ opacity: item.status === 'pending' ? 0.72 : 1 }}
    >
      <Flex align="center" gap={6}>
        <Clock size={12} />
        <span className="queue-strip__content" title={item.content}>
          {item.content}
        </span>
      </Flex>
      <Flex gap={4} align="center" justify="space-between">
        <Flex gap={8} align="center">
          {statusTag}
          {item.attachments.length ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {item.attachments.length} 个附件
            </Typography.Text>
          ) : null}
          {item.kbs.length ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {item.kbs.map((k) => k.name).join('、')}
            </Typography.Text>
          ) : null}
        </Flex>
        <Flex gap={2} align="center">
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
    </Flex>
  )
}

export function QueueList({ sessionId }: { sessionId: UUID }): ReactNode {
  const items = useChatStore((s) => s.queueItems)
  const queueState = useChatStore((s) => s.queueState)
  const pausedReason = useChatStore((s) => s.queuePausedReason)
  const syncQueue = useChatStore((s) => s.syncQueue)
  const [busy, setBusy] = useState(false)

  if (queueState === 'idle' && !items.length) return null

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
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            待发队列（{pending.length}，按顺序自动发送）
          </Typography.Text>
          {pending.map((item) => (
            <QueueRow key={item.id} item={item} sessionId={sessionId} />
          ))}
        </div>
      ) : null}
      {salvage.length ? (
        <div className="queue-strip__group">
          <Flex gap={8} align="center">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              已取消 / 失败（{salvage.length}）
            </Typography.Text>
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
