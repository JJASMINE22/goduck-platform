/* 会话回合导航轨（TurnRail）—— 每轮对话一个 label：悬停弹小窗预览该轮内容，点击跳转。
   - turns 由 chatStore.messages 派生（role==='user' 切分回合）：label = 用户 query 截断；
     预览 = query 全文 + 该轮首条非空代理回复（流式中显示已到部分）。
     历史灌入与实时流共用同一 store，刷新后自动重建；锚点与 label 同源同快照，不会错位。
   - 点击经 data-turn-anchor 锚点平滑滚动（锚点挂在 MessageList 的用户回合容器上）；
     跳转后 scroll 容器判定离底、自动追底停止——复用 ChatPage 既有 pinnedRef 机制。
   - 三级形态由容器查询驱动（.chat-panel 为容器，见 index.css）：
     宽面板 = 完整 label / 中宽 = 仅圆点（悬停仍有预览）/ 窄面板 = 整轨隐藏；
     阅读列同步在右侧让位，轨道永不与气泡及回撤操作条重叠。
   - <3 回合不渲染：短会话零打扰。 */
import { Popover } from 'antd'
import { type ReactNode, type RefObject, useMemo } from 'react'
import { useChatStore } from '@/store/chatStore'
import type { ChatMessage } from '@/store/chatStore'

interface Turn {
  anchorId: string
  /** 轨道内单行截断展示（刻意短于预览） */
  label: string
  /** 预览卡里的 query 全文（截断上限宽松） */
  queryText: string
  /** 该轮首条非空代理回复（空串 = 尚无回复，预览卡省略该区） */
  replyPreview: string
}

/** 出现阈值：回合数不足时整轨不渲染（短会话零打扰） */
const MIN_TURNS = 3
const LABEL_MAX = 26
const QUERY_MAX = 120
const REPLY_MAX = 200

/** 压平连续空白后按上限截断，超限补省略号。 */
function truncate(text: string, max: number): string {
  const t = text.trim().replace(/\s+/g, ' ')
  return t.length > max ? `${t.slice(0, max)}…` : t
}

/** messages → 回合列表：用户消息开回合，其后至下一用户消息前归属同回合。
    replyPreview 取回合内首条 content 非空的代理消息（思考块 content 恒空，自然跳过）。 */
function deriveTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = []
  let current: Turn | null = null
  for (const m of messages) {
    if (m.role === 'user') {
      if (current) turns.push(current)
      const raw = truncate(m.content, QUERY_MAX)
      current = {
        anchorId: m.id,
        label: truncate(m.content, LABEL_MAX) || (m.attachments?.length ? '（附件）' : '（空）'),
        queryText: raw || '（空）',
        replyPreview: '',
      }
    } else if (current && !current.replyPreview && m.content.trim()) {
      current.replyPreview = truncate(m.content, REPLY_MAX)
    }
  }
  if (current) turns.push(current)
  return turns
}

/** data-turn-anchor 属性选择器（转义引号/反斜杠，防御消息 id 意外包含选择器语法）。 */
function anchorSelector(id: string): string {
  return `[data-turn-anchor="${id.replace(/["\\]/g, '\\$&')}"]`
}

export function TurnRail({ scrollRef }: { scrollRef: RefObject<HTMLDivElement | null> }): ReactNode {
  const messages = useChatStore((s) => s.messages)
  // 仅在 messages 引用变化时重算；流式逐 token 会触发，但 O(n) 浅遍历开销可忽略
  const turns = useMemo(() => deriveTurns(messages), [messages])

  if (turns.length < MIN_TURNS) return null

  const jump = (anchorId: string) => {
    const anchor = scrollRef.current?.querySelector(anchorSelector(anchorId))
    anchor?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <nav className="turn-rail" aria-label="回合导航">
      {turns.map((t, i) => (
        <Popover
          key={t.anchorId}
          trigger="hover"
          placement="left"
          arrow={false}
          mouseEnterDelay={0.2}
          mouseLeaveDelay={0.1}
          overlayClassName="turn-rail__popover"
          overlayStyle={{ maxWidth: 340 }}
          content={
            <div className="turn-rail__preview">
              <div className="turn-rail__preview-seq">第 {i + 1} 轮</div>
              <div className="turn-rail__preview-query">{t.queryText}</div>
              {t.replyPreview ? (
                <div className="turn-rail__preview-reply">{t.replyPreview}</div>
              ) : null}
            </div>
          }
        >
          <button
            type="button"
            className="turn-rail__item"
            onClick={() => jump(t.anchorId)}
            aria-label={`跳转到第 ${i + 1} 轮`}
          >
            <span className="turn-rail__dot" />
            <span className="turn-rail__label">{t.label}</span>
          </button>
        </Popover>
      ))}
    </nav>
  )
}
