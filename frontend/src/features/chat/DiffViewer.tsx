/* 行级红绿 diff 明细视图（公共渲染件）—— ToolCallCard（MCP 工具结果 diff）与
   FileChangesCard（file_change 明细）共用：DiffLine[] 逐行染色（tc-diff__line--*，
   与 git diff 观感同源），默认折叠钳制（渐隐遮罩 + 展开全部，与 JSON 载荷同规则）。 */
import { type ReactNode, useState } from 'react'
import type { DiffLine } from './diffResult'

const CLAMP_LINES = 10
const CLAMP_MAX_HEIGHT = 200

export function DiffViewer({ lines }: { lines: DiffLine[] }): ReactNode {
  const [expanded, setExpanded] = useState(false)
  const foldable = lines.length > CLAMP_LINES
  const clamped = foldable && !expanded
  const shown = clamped ? lines.slice(0, CLAMP_LINES + 2) : lines
  return (
    <>
      <pre
        className={`tc-payload__pre tc-payload__pre--detail ${clamped ? 'tc-payload__pre--clamp' : ''}`}
        style={clamped ? { maxHeight: CLAMP_MAX_HEIGHT } : undefined}
      >
        <code className="tc-diff">
          {shown.map((l, i) => (
            <span key={i} className={`tc-diff__line tc-diff__line--${l.type}`}>
              {l.text}
              {'\n'}
            </span>
          ))}
        </code>
      </pre>
      {foldable ? (
        <button type="button" className="tc-payload__toggle" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : `展开全部（共 ${lines.length} 行）`}
        </button>
      ) : null}
    </>
  )
}
