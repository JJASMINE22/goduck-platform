/* 技能上传目录结构支持：将拖拽/选择的文件夹递归展开为扁平 { file, path } 列表。
   - 拖拽文件夹：通过 DataTransferItem.webkitGetAsEntry() 递归遍历目录
   - 选择文件夹（webkitdirectory input）：File 上带 webkitRelativePath
   - 普通文件：直接使用 file.name 作为相对路径
   目录本身不能作为 FormData part 读取（会导致 fetch 抛 "Failed to fetch"），
   因此必须先展开成具体文件。 */
export interface FlatFileEntry {
  file: File
  /** 相对技能根目录的路径，如 "scripts/run.js"；后端据此重建子目录。 */
  path: string
}

/** 规范化相对路径：统一分隔符为 "/"，去掉前导斜杠与 "." 段，阻断绝对路径/穿越。 */
export function normalizeRelPath(p: string): string {
  return p
    .replace(/\\/g, '/')
    .split('/')
    .filter((seg) => seg && seg !== '.')
    .join('/')
}

/** 从 webkitdirectory <input> 选中的文件提取相对路径（webkitRelativePath）。
 *  注意：仅「选择文件夹」会带 webkitRelativePath（首段为所选文件夹名，需剥离）；
 *  普通「选择文件」无该属性，直接用文件名，绝不丢失任何子目录层级。 */
export function fromInputFiles(fileList: FileList | null): FlatFileEntry[] {
  if (!fileList) return []
  const out: FlatFileEntry[] = []
  for (const f of Array.from(fileList)) {
    const wrp = (f as File & { webkitRelativePath?: string }).webkitRelativePath ?? ''
    let path = normalizeRelPath(wrp || f.name)
    // 仅文件夹选择器会带相对路径，且首段即所选文件夹名——剥离它（其余层级原样保留）
    if (wrp && path.includes('/')) {
      path = path.split('/').slice(1).join('/')
    }
    if (path) out.push({ file: f, path })
  }
  return out
}

/**
 * 仅当「所有条目共享的顶层目录」与技能名一致时，才剥离该顶层目录。
 * 用于拖拽「整个技能文件夹」的情形：拖入 amap-lbs-skill/（技能名同为 amap-lbs-skill）
 * → 剥掉 amap-lbs-skill/ 前缀；而 scripts/ 这类真正子目录（≠ 技能名）绝不剥离，
 * 从而既消除外层文件夹多带一层，又保证 scripts/ 等层级不丢失。
 */
export function stripWrapperMatchingName(entries: FlatFileEntry[], name: string): FlatFileEntry[] {
  if (!entries.length || !name) return entries
  const segs = entries.map((e) => e.path.split('/'))
  // 必须所有条目都嵌套在同一顶层目录下
  if (!segs.every((s) => s.length > 1)) return entries
  const top = segs[0][0]
  if (top === name && segs.every((s) => s[0] === top)) {
    return entries.map((e, i) => ({ file: e.file, path: segs[i].slice(1).join('/') }))
  }
  return entries
}

function readEntryDirectory(entry: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  return new Promise((resolve) => {
    // createReader + readEntries 需循环调用直到返回空数组（分批读取）
    const reader = entry.createReader()
    const all: FileSystemEntry[] = []
    const readBatch = () => {
      reader.readEntries(
        (batch) => {
          if (!batch.length) {
            resolve(all)
            return
          }
          all.push(...batch)
          readBatch()
        },
        () => resolve(all),
      )
    }
    readBatch()
  })
}

function entryToFile(entry: FileSystemFileEntry): Promise<File | null> {
  return new Promise((resolve) => {
    entry.file(
      (f) => resolve(f),
      () => resolve(null),
    )
  })
}

async function traverseEntry(
  entry: FileSystemEntry,
  prefix: string,
  acc: FlatFileEntry[],
): Promise<void> {
  if (entry.isFile) {
    const file = await entryToFile(entry as FileSystemFileEntry)
    if (file) acc.push({ file, path: normalizeRelPath(`${prefix}/${entry.name}`) })
    return
  }
  if (entry.isDirectory) {
    const children = await readEntryDirectory(entry as FileSystemDirectoryEntry)
    for (const child of children) {
      await traverseEntry(child, `${prefix}/${entry.name}`, acc)
    }
  }
}

/** 将拖拽事件中的 DataTransfer 递归展开为扁平文件列表（支持嵌套文件夹）。 */
export async function fromDataTransfer(dt: DataTransfer): Promise<FlatFileEntry[]> {
  const items = dt.items ? Array.from(dt.items) : []
  const acc: FlatFileEntry[] = []

  // 优先走 entry API（能区分文件/目录并递归展开文件夹）
  if (items.length && typeof items[0].webkitGetAsEntry === 'function') {
    const entries = items
      .map((it) => it.webkitGetAsEntry?.())
      .filter((e): e is FileSystemEntry => !!e)
    if (entries.length) {
      for (const entry of entries) {
        await traverseEntry(entry, '', acc)
      }
      if (acc.length) return acc
    }
  }

  // 回退：普通文件（无法展开目录，按文件名处理）
  if (dt.files?.length) {
    for (const f of Array.from(dt.files)) acc.push({ file: f, path: normalizeRelPath(f.name) })
  }
  return acc
}
