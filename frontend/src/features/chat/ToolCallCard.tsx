/* 工具调用卡（聊天时间线）—— 参数/结果的友好展示。
    结果常为「JSON 字符串再包一层」（含字面 \n 转义）：先解包再美化缩进；
    JSON 轻量语法高亮（键/字符串/数字/布尔分色，零依赖正则词法）；
    超长内容默认折叠（渐隐遮罩 + 展开全部），参数/结果各带一键复制。
    执行中：工具名亮银色流光（与思考标签同一「进行中」语言，准则 F7）。 */
import { Collapse, Flex, Tag, Typography } from 'antd'
import { type ReactNode, useMemo, useState } from 'react'
import { Check, Wrench } from 'lucide-react'
import type { ToolCallEntry } from '@/store/chatStore'

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

/** 一段载荷（参数或结果）：标题栏（标签 + 行数 + 复制）+ 高亮正文 + 展开/收起。
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

export function ToolCallCard({ entry }: { entry: ToolCallEntry }): ReactNode {
  // 参数本身是对象：直接美化（与结果走同一渲染管线）
  const argsText = useMemo(() => JSON.stringify(entry.args, null, 2) || '{}', [entry.args])
  const resultText = useMemo(() => (entry.hasResult ? formatPayload(entry.result ?? '') : ''), [entry.hasResult, entry.result])
  return (
    <Flex justify="flex-start" align="flex-start" gap={10} className="msg-in">
      <div className="chat-avatar" style={{ background: 'var(--color-role-tool)', width: 28, height: 28 }}>
        {entry.hasResult ? <Check size={14} /> : <Wrench size={14} className="executing" />}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
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
                  ) : (
                    <Tag color="processing" style={{ marginInlineStart: 4 }}>执行中</Tag>
                  )}
                </Flex>
              ),
              children: (
                <Flex vertical gap={8}>
                  <PayloadSection title="参数" text={argsText} />
                  {entry.hasResult ? (
                    <PayloadSection title="结果" text={resultText || '（空）'} />
                  ) : null}
                </Flex>
              ),
            },
          ]}
        />
      </div>
    </Flex>
  )
}
