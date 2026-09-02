/* 会话级 token 用量徽标 —— 挂在会话页 Header 卡标题区（与状态 Tag 同层的元信息）。
   数据：chatStore.usageTotals（usage 事件逐条累加；基线来自 GET /sessions/{id}
   的 usage_totals，流结束终态失效后以服务端真值校正）。
   视觉：流体玻璃胶囊（Liquid Glass，与 .glass-btn 同源三要素：低 α 半透底 +
   backdrop-filter 取景 + rim 高光/镜面亮斑，样式见 index.css .usage-badge）：
   - ↑输入 / ↓输出：次要文本色；
   - ⚡缓存命中：success 绿 + 半粗（用户重点关注的成本项，主行直接可见，Tooltip
     另给命中率与思考 token 明细）；
   - tabular-nums 防数字跳动撑宽布局；增量到达让玻璃"凝亮"（--bump：rim 抬高 +
     主色淡辉光），尺寸恒定不挤压相邻 Tag。全零（新会话）整体隐藏，干净入场。 */
import { Tooltip } from 'antd'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Zap } from 'lucide-react'
import { useChatStore } from '@/store/chatStore'
import type { UsageTotals } from '@/types'

/** 紧凑数字：<1k 原样；<1M 保留一位小数的 k；再往上 M（token 语境惯用 k/M） */
function fmt(n: number): string {
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${+(n / 1e3).toFixed(1)}k`
  return `${+(n / 1e6).toFixed(1)}M`
}

function UsageBadgeRow({ label, value, suffix }: { label: string; value: string; suffix?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 24, minWidth: 150 }}>
      <span style={{ color: 'var(--color-text-secondary)' }}>{label}</span>
      <span style={{ fontVariantNumeric: 'tabular-nums' }}>
        {value}
        {suffix ? <span style={{ color: 'var(--color-text-tertiary)' }}>（{suffix}）</span> : null}
      </span>
    </div>
  )
}

/** Tooltip 明细：缓存命中为 input 的子集，输入行按"非缓存部分"展示避免双算 */
function detailRows(t: UsageTotals): ReactNode {
  const missRate = t.input_tokens > 0 ? `${Math.round((t.cached_tokens / t.input_tokens) * 100)}% 命中` : undefined
  const reasoning = t.reasoning_tokens > 0 ? fmt(t.reasoning_tokens) : undefined
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12 }}>
      <UsageBadgeRow label="输入" value={fmt(t.input_tokens - t.cached_tokens)} />
      <UsageBadgeRow label="缓存命中" value={fmt(t.cached_tokens)} suffix={missRate} />
      <UsageBadgeRow label="输出" value={fmt(t.output_tokens)} suffix={reasoning ? `含思考 ${reasoning}` : undefined} />
      <UsageBadgeRow label="总计" value={fmt(t.total_tokens)} />
    </div>
  )
}

export function UsageBadge(): ReactNode {
  const totals = useChatStore((s) => s.usageTotals)
  // 增量脉冲：total 变化 → 短暂提亮再回落（transition 承担渐变，定时器只负责回落）
  const [bump, setBump] = useState(false)
  const prevTotal = useRef(totals.total_tokens)
  useEffect(() => {
    if (totals.total_tokens === prevTotal.current) return
    prevTotal.current = totals.total_tokens
    setBump(true)
    const t = setTimeout(() => setBump(false), 400)
    return () => clearTimeout(t)
  }, [totals.total_tokens])

  if (totals.total_tokens === 0) return null

  return (
    <Tooltip title={detailRows(totals)} mouseEnterDelay={0.2}>
      <span className={`usage-badge${bump ? ' usage-badge--bump' : ''}`}>
        <span className="usage-badge__seg">
          <ArrowUp size={12} />
          {fmt(totals.input_tokens)}
        </span>
        <span className="usage-badge__seg usage-badge__seg--cache" title="缓存命中">
          <Zap size={12} />
          {fmt(totals.cached_tokens)}
        </span>
        <span className="usage-badge__seg">
          <ArrowDown size={12} />
          {fmt(totals.output_tokens)}
        </span>
      </span>
    </Tooltip>
  )
}
