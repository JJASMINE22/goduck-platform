/* 工具结果 diff 识别与解析（纯函数；组件文件只导出组件的纪律同 kbSearchTool.ts）。
   背景：MCP 文件编辑类工具直接写宿主路径（不经工作区 TrackedFileScopedBackend
   埋点），无 file_change SSE 事件 → 无"文件变更"聚合卡与 +/− 计数；但这类工具
   的结果本身是 unified diff（git/svn 风格，常被 markdown ```diff 围栏包裹）——
   在此识别并结构化为"文件级总览 + 行级明细"两层：
   - 总览：按 Index: / diff --git 头分文件组，hunk 头推断动作（@@ -0,0 → 新建、
     +0,0 → 删除），逐文件 +N/−M 计数——与 file_change 聚合卡同语义；
   - 明细：逐行分类（add/del/hunk/meta/ctx），供染色视图渲染。
   两路观测互补：工作区后备编辑 → file_change 聚合卡（不变）；MCP/直写 → 本路。 */

export interface DiffLine {
  type: 'add' | 'del' | 'hunk' | 'meta' | 'ctx'
  text: string
}

export interface DiffFile {
  /** 规范化路径（\ → /；git 的 a/ b/ 前缀已剥；无头裸 diff 兜底"未知文件"） */
  path: string
  action: 'create' | 'edit' | 'delete'
  added: number
  removed: number
  /** 该文件区段的行（含 Index/===/---/+++ 头），明细视图按文件渲染 */
  lines: DiffLine[]
}

export interface ParsedDiff {
  files: DiffFile[]
  /** 全量行（跨文件连续），整卡明细视图直接消费 */
  lines: DiffLine[]
  added: number
  removed: number
}

/** 剥掉 markdown 代码围栏（```diff / ``` 包裹的整段）：围栏行不是 diff 内容，
 *  染色与计数都必须排除。非围栏形态原样返回。 */
function stripFences(text: string): string {
  const t = text.trim()
  if (!t.startsWith('```')) return t
  const firstNl = t.indexOf('\n')
  if (firstNl < 0 || !t.endsWith('```')) return t
  return t.slice(firstNl + 1, -3).replace(/\n$/, '')
}

/** 是否 unified diff：要求同时具备 @@ hunk 头、+++ / --- 文件对、+ 或 - 变更行
 *  （三者齐全误报率极低——JSON/普通文本不会同时满足；流式半截时计数不全但
 *  结构判定仍成立，结果落定后重渲染校正）。 */
export function isUnifiedDiff(text: string): boolean {
  const lines = stripFences(text).split('\n')
  if (lines.length < 3) return false
  let hunks = 0
  let markers = 0
  let adds = 0
  let dels = 0
  for (const l of lines) {
    if (l.startsWith('@@')) hunks++
    else if (l.startsWith('+++ ') || l.startsWith('--- ')) markers++
    else if (l.startsWith('+')) adds++
    else if (l.startsWith('-')) dels++
  }
  return hunks > 0 && markers >= 2 && adds + dels > 0
}

/** 宿主绝对路径（盘符 / 斜杠开头）：平台工作区在独立存储（Docker 卷），
 *  这类文件不在工作区内、无法跳转打开——前端据此降级为不可点行。 */
export function isAbsoluteHostPath(path: string): boolean {
  return /^[A-Za-z]:\//.test(path) || path.startsWith('/')
}

/** 从 ---/+++ 标记行取路径并规范化：git 剥 a/ b/ 前缀；svn 剥行尾 tab+original/
 *  modified/revision 标注；反斜杠统一为 /。 */
function pathFromMarker(line: string): string | null {
  const raw = line.slice(4)
  if (!raw || raw === '/dev/null') return null
  const noMeta = raw.split('\t')[0].trim()
  const stripped = noMeta.replace(/^(a|b)\//, '')
  return stripped.replace(/\\/g, '/') || null
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

/** 单文件区段分类 + 计数 + 动作推断（hunk 头优先：-0 起始=新建、+0 起始=删除；
 *  头缺失/不可解析时按纯增/纯删回退）。 */
function classifyGroup(lines: string[], path: string): DiffFile {
  const out: DiffLine[] = []
  let added = 0
  let removed = 0
  let oldStart: number | null = null
  let newStart: number | null = null
  for (const l of lines) {
    if (l.startsWith('@@')) {
      const m = HUNK_RE.exec(l)
      if (m) {
        if (oldStart === null) oldStart = Number(m[1])
        if (newStart === null) newStart = Number(m[2])
      }
      out.push({ type: 'hunk', text: l })
    } else if (
      l.startsWith('+++ ') ||
      l.startsWith('--- ') ||
      l.startsWith('Index: ') ||
      l.startsWith('===') ||
      l.startsWith('diff --git ') ||
      l.startsWith('index ')
    ) {
      out.push({ type: 'meta', text: l })
    } else if (l.startsWith('+')) {
      added++
      out.push({ type: 'add', text: l })
    } else if (l.startsWith('-')) {
      removed++
      out.push({ type: 'del', text: l })
    } else {
      out.push({ type: 'ctx', text: l })
    }
  }
  const action: DiffFile['action'] =
    oldStart === 0 || newStart === 0
      ? oldStart === 0
        ? 'create'
        : 'delete'
      : oldStart === null && removed === 0
        ? 'create'
        : oldStart === null && added === 0
          ? 'delete'
          : 'edit'
  return { path, action, added, removed, lines: out }
}

/** 解析多文件 unified diff：按 Index:（svn）/ diff --git（git）头分组；
 *  无分组头的裸 diff（仅 ---/+++ 对）作单文件处理。 */
export function parseDiff(text: string): ParsedDiff {
  const raw = stripFences(text).split('\n')
  const groups: Array<{ path: string | null; lines: string[] }> = []
  let cur: { path: string | null; lines: string[] } | null = null
  for (const l of raw) {
    if (l.startsWith('Index: ')) {
      cur = { path: l.slice(7).trim().replace(/\\/g, '/') || null, lines: [l] }
      groups.push(cur)
      continue
    }
    if (l.startsWith('diff --git ')) {
      cur = { path: null, lines: [l] } // 路径等 +++ b/... 行到达时回填
      groups.push(cur)
      continue
    }
    if (cur && cur.path === null && (l.startsWith('+++ ') || l.startsWith('--- '))) {
      cur.path = pathFromMarker(l) ?? cur.path
    }
    if (!cur) {
      cur = { path: null, lines: [] }
      groups.push(cur)
    }
    cur.lines.push(l)
  }
  const files = groups
    .filter((g) => g.lines.some((l) => l.startsWith('@@')))
    .map((g) => classifyGroup(g.lines, g.path ?? '未知文件'))
  // 兜底：分组头一个都没有（异常形态）→ 整体单文件，避免总览空转
  if (!files.length) files.push(classifyGroup(raw, '未知文件'))
  const lines = files.flatMap((f) => f.lines)
  return {
    files,
    lines,
    added: files.reduce((s, f) => s + f.added, 0),
    removed: files.reduce((s, f) => s + f.removed, 0),
  }
}
