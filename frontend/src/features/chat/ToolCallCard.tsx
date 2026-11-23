/* 工具调用卡（聊天时间线）—— 参数/结果的友好展示。
    结果常为「JSON 字符串再包一层」（含字面 \n 转义）：先解包再美化缩进；
    JSON 轻量语法高亮（键/字符串/数字/布尔分色，零依赖正则词法）；
    超长内容默认折叠（渐隐遮罩 + 展开全部），参数/结果各带一键复制。
    结果为 unified diff（MCP 文件编辑工具直写宿主路径、无 file_change 事件）：
    总览层 = 文件行（动作 + +N/−M + 可点跳工作区）+ Diff 图标展开红绿明细
    （diffResult.ts，与 file_change 聚合卡的观测语义同源互补）。
    执行中：工具名亮银色流光（与思考标签同一「进行中」语言，准则 F7）。 */
import { Collapse, Flex, Tag, Tooltip, Typography } from 'antd'
import { type ReactNode, memo, useMemo, useState } from 'react'
import { Check, Diff, FileMinus, FilePen, FilePlus, Wrench } from 'lucide-react'
import { isUnifiedDiff, isAbsoluteHostPath, parseDiff, type ParsedDiff } from './diffResult'
import { DiffViewer } from './DiffViewer'
import type { ToolCallEntry } from '@/store/chatStore'

/** diff 动作 → 行图标/标签（与 FileChangesCard.ACTION_META 同语义；
 *  本地重定义而非导出复用——组件文件只导出组件的纪律） */
const DIFF_ACTION_META = {
  create: { label: '新建', Icon: FilePlus },
  edit: { label: '编辑', Icon: FilePen },
  delete: { label: '删除', Icon: FileMinus },
} as const

const basename = (path: string): string => path.split('/').pop() ?? path

/** 折叠阈值：超过该行数默认收起（结果可能数千行，裸撑爆时间线） */
const CLAMP_LINES = 10
/** 折叠态视高上限（px）：约 CLAMP_LINES × 正文行高，与 CSS 渐隐遮罩配合 */
const CLAMP_MAX_HEIGHT = 200

/** 解包「JSON 字符串包 JSON」（含字面 \n 转义的双重编码）并美化缩进；
    非 JSON（或半截 JSON）原样返回。限深防病态嵌套。 */
function formatPayload(raw: string, depth = 0): string {
  const s = raw.trim()
  if (!s) return ''
  if (s.startsWith('"') || s.startsWith('{') || s.startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(s)
      if (typeof parsed === 'string') return depth < 3 ? formatPayload(parsed, depth + 1) : parsed
      if (typeof parsed === 'object' && parsed !== null) return JSON.stringify(parsed, null, 2)
      return String(parsed)
    } catch {
      /* 流式截断/拼接产生的半截 JSON：保持原样 */
    }
  }
  return s
}

/** JSON 词法切片：字符串（后跟冒号=键）/ 数字 / 布尔与 null / 其余（标点/空白）原色 */
function tokenizeJson(src: string): Array<{ text: string; cls?: string }> {
  const re = /"(?:\\.|[^"\\])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g
  const out: Array<{ text: string; cls?: string }> = []
  let last = 0
  for (const m of src.matchAll(re)) {
    const idx = m.index ?? 0
    if (idx > last) out.push({ text: src.slice(last, idx) })
    const t = m[0]
    if (t.endsWith(':')) out.push({ text: t, cls: 'tc-json__key' })
    else if (t.startsWith('"')) out.push({ text: t, cls: 'tc-json__str' })
    else if (t === 'true' || t === 'false' || t === 'null') out.push({ text: t, cls: 'tc-json__bool' })
    else out.push({ text: t, cls: 'tc-json__num' })
    last = idx + t.length
  }
  if (last < src.length) out.push({ text: src.slice(last) })
  return out
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

/** 一段载荷（参数或 JSON 结果）：标题栏（标签 + 行数 + 复制）+ 高亮正文 + 展开/收起。
    折叠态只渲染前几行（数千行的结果一次性 tokenize + 建 DOM 会卡时间线）。 */
function PayloadSection({ title, text }: { title: string; text: string }): ReactNode {
  const lines = useMemo(() => text.split('\n').length, [text])
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)
  const foldable = lines > CLAMP_LINES
  const clamped = foldable && !expanded
  const displayText = useMemo(
    () => (clamped ? text.split('\n').slice(0, CLAMP_LINES + 2).join('\n') : text),
    [text, clamped],
  )
  const tokens = useMemo(() => tokenizeJson(displayText), [displayText])
  const onCopy = async () => {
    if (await copyText(text)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="tc-payload">
      <div className="tc-payload__bar">
        <span className="tc-payload__title">{title}</span>
        <span className="tc-payload__count">{lines} 行</span>
        <button type="button" className="tc-payload__btn" onClick={onCopy}>
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre
        className={`tc-payload__pre ${clamped ? 'tc-payload__pre--clamp' : ''}`}
        style={clamped ? { maxHeight: CLAMP_MAX_HEIGHT } : undefined}
      >
        <code className="tc-json">
          {tokens.map((t, i) =>
            t.cls ? (
              <span key={i} className={t.cls}>
                {t.text}
              </span>
            ) : (
              t.text
            ),
          )}
        </code>
      </pre>
      {foldable ? (
        <button type="button" className="tc-payload__toggle" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : `展开全部（共 ${lines} 行）`}
        </button>
      ) : null}
    </div>
  )
}

/** diff 结果的总览层：文件行（动作 + 文件名 + +N/−M，可点跳工作区）+ 明细切换。
    - 总览默认呈现（用户只需要"哪些文件变了"）；明细按钮展开逐行染色的 diff 视图，
      折叠钳制与 JSON 载荷同规则（CLAMP_LINES + 渐隐 + 展开全部）；
    - 跳转条件：onOpenFile 可用 ∧ 路径为工作区相对路径（宿主绝对路径不在平台
      工作区存储内，物理不可达——降级为不可点行，Tooltip 说明原因）。 */
function DiffSummarySection({
  text,
  diff,
  onOpenFile,
}: {
  text: string
  diff: ParsedDiff
  onOpenFile?: (path: string) => void
}): ReactNode {
  const [showDetail, setShowDetail] = useState(false)
  const [copied, setCopied] = useState(false)
  const totalLines = diff.lines.length
  const onCopy = async () => {
    if (await copyText(text)) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <div className="tc-payload">
      <div className="tc-payload__bar">
        <span className="tc-payload__title">文件变更</span>
        <span className="tc-payload__count">
          {diff.files.length} 个文件 · {totalLines} 行
        </span>
        <span className="tc-payload__stat tc-payload__stat--add">+{diff.added}</span>
        <span className="tc-payload__stat tc-payload__stat--del">−{diff.removed}</span>
        <button type="button" className="tc-payload__btn" onClick={() => setShowDetail((v) => !v)}>
          <Diff size={11} style={{ verticalAlign: -1 }} />
          {showDetail ? '收起明细' : '明细'}
        </button>
        <button type="button" className="tc-payload__btn" onClick={onCopy}>
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <div className="fc-list">
        {diff.files.map((f) => {
          const deleted = f.action === 'delete'
          const clickable = !!onOpenFile && !deleted && !isAbsoluteHostPath(f.path)
          const { label, Icon } = DIFF_ACTION_META[f.action]
          const tip = isAbsoluteHostPath(f.path)
            ? `${f.path}（外部工具直写宿主路径，不在平台工作区内，无法跳转）`
            : f.path
          return (
            <Tooltip key={f.path} title={tip}>
              <div
                className={`fc-row ${deleted ? 'fc-row--deleted' : ''} ${clickable ? 'fc-row--clickable' : ''}`}
                onClick={clickable ? () => onOpenFile!(f.path) : undefined}
              >
                <Icon size={13} className={`fc-row__icon fc-row__icon--${f.action}`} />
                <span className="fc-row__action">{label}</span>
                <span className={`fc-row__path ${deleted ? 'fc-row__path--struck' : ''}`}>
                  {basename(f.path)}
                </span>
                <span className="fc-row__num fc-row__num--add">{f.added ? `+${f.added}` : ''}</span>
                <span className="fc-row__num fc-row__num--del">{f.removed ? `−${f.removed}` : ''}</span>
              </div>
            </Tooltip>
          )
        })}
      </div>
      {showDetail ? <DiffViewer lines={diff.lines} /> : null}
    </div>
  )
}

/** 工具调用卡（memo：entry 引用仅在该卡自身更新（setToolResult 回填结果）时
    变化；onOpenFile 由 ChatPage useCallback 稳定供给——流式批渲染时其余工具卡跳过） */
export const ToolCallCard = memo(function ToolCallCard({
  entry,
  onOpenFile,
}: {
  entry: ToolCallEntry
  /** diff 总览行点击 → 打开工作区并选中该文件（未开启 workspace 能力时不传；
   *  宿主绝对路径行仍不可点——文件不在平台工作区存储内） */
  onOpenFile?: (path: string) => void
}): ReactNode {
  // 参数本身是对象：直接美化（与结果走同一渲染管线）
  const argsText = useMemo(() => JSON.stringify(entry.args, null, 2) || '{}', [entry.args])
  const resultText = useMemo(() => (entry.hasResult ? formatPayload(entry.result ?? '') : ''), [entry.hasResult, entry.result])
  // 结果是 unified diff → 总览层渲染（文件行 + 明细切换）；否则 JSON 载荷
  const resultDiff = useMemo<ParsedDiff | null>(
    () => (entry.hasResult && resultText && isUnifiedDiff(resultText) ? parseDiff(resultText) : null),
    [entry.hasResult, resultText],
  )
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)' }}>
        {entry.hasResult ? (
          <Check size={14} />
        ) : (
          /* 已中断：静态图标（不脉动）；执行中：脉动 */
          <Wrench size={14} className={entry.abandoned ? undefined : 'executing'} />
        )}
      </div>
      <div className="msg-card msg-card--tool">
        <Collapse
          size="small"
          items={[
            {
              key: entry.id,
              label: (
                /* 未返回（执行中）：工具名文字做亮银色流光，与思考标签同一「进行中」语言 */
                <Flex align="center" gap={6}>
                  <code className={entry.hasResult ? undefined : 'shimmer'} style={{ fontSize: 12 }}>
                    {entry.name}
                  </code>
                  {entry.caller ? (
                    <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                      by {entry.caller}
                    </Typography.Text>
                  ) : null}
                  {entry.hasResult ? (
                    <Tag color="green" style={{ marginInlineStart: 4 }}>已返回</Tag>
                  ) : entry.abandoned ? (
                    <Tag style={{ marginInlineStart: 4 }}>已中断</Tag>
                  ) : (
                    <Tag color="processing" style={{ marginInlineStart: 4 }}>执行中</Tag>
                  )}
                </Flex>
              ),
              children: (
                <Flex vertical gap={8}>
                  <PayloadSection title="参数" text={argsText} />
                  {entry.hasResult ? (
                    resultDiff ? (
                      <DiffSummarySection text={resultText} diff={resultDiff} onOpenFile={onOpenFile} />
                    ) : (
                      <PayloadSection title="结果" text={resultText || '（空）'} />
                    )
                  ) : null}
                </Flex>
              ),
            },
          ]}
        />
      </div>
    </Flex>
  )
})
