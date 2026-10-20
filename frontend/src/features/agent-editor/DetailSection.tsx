/* 配置分区卡（方案 A）+ 短字段开关格（方案 C）—— 节点详情面板的展示骨架。
   DetailSection：sticky 玻璃区头（图标圆徽 + 标题 + 摘要徽标 + 折叠钮）+ 双列
   网格体（auto-fit：窄面板单列、宽面板双列；长字段以 detail-field--wide 整行）。
   分区色经 --sec-tone 下发到子树：区头圆徽、开启态开关格（detail-cell--on）
   的色带与染色同源。折叠仅收起体，区头摘要保证折叠态信息不丢。
   SwitchField：开关格单元（标签 + 开关 + 次级说明），开启时点亮分区色带，
   替代旧"标签行 + 开关 + 长说明"的均质堆叠。 */
import { Switch } from 'antd'
import { type CSSProperties, type ReactNode, useState } from 'react'
import { ChevronDown } from 'lucide-react'

export function DetailSection({
  icon,
  title,
  summary,
  tone,
  children,
}: {
  icon: ReactNode
  title: string
  /** 区头右侧摘要徽标（如 "工具 3 · 审批 1"），随配置实时变化 */
  summary?: string
  /** 分区色（CSS color 值，通常为 var(--xxx)；缺省主色） */
  tone?: string
  children: ReactNode
}): ReactNode {
  const [collapsed, setCollapsed] = useState(false)
  return (
    <section
      className={collapsed ? 'detail-section detail-section--collapsed' : 'detail-section'}
      style={(tone ? { '--sec-tone': tone } : undefined) as unknown as CSSProperties}
    >
      <header className="detail-section__head">
        <span className="detail-section__icon">{icon}</span>
        <span className="detail-section__title">{title}</span>
        {summary ? <span className="detail-section__summary">{summary}</span> : null}
        <button
          type="button"
          className="detail-section__toggle"
          data-collapsed={collapsed ? 'true' : 'false'}
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? '展开分区' : '折叠分区'}
          aria-expanded={!collapsed}
        >
          <ChevronDown size={14} />
        </button>
      </header>
      {collapsed ? null : <div className="detail-section__body">{children}</div>}
    </section>
  )
}

/** 开关格：标签 + 开关（同行）+ 次级说明（下方小字）；开启态点亮分区色 */
export function SwitchField({
  label,
  desc,
  checked,
  onChange,
  disabled,
}: {
  label: string
  desc?: string
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}): ReactNode {
  return (
    <div className={checked ? 'detail-cell detail-cell--on' : 'detail-cell'}>
      <div className="detail-cell__head">
        <span className="detail-cell__label">{label}</span>
        <Switch size="small" checked={checked} onChange={onChange} disabled={disabled} />
      </div>
      {desc ? <div className="detail-cell__desc">{desc}</div> : null}
    </div>
  )
}
