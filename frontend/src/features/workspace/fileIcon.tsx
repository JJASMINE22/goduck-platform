/* 工作区树类型图标 —— 扩展名 → lucide 图标 + 语义色（主题变量着色，深浅色自适应）。
    扩展名口径与预览分派（fileKind.classifyFile）同源（extOf 共享），但粒度更细：
    代码 / 配置 / Shell / 压缩包 / 音视频等各自单列，只服务观感，不影响预览策略。
    色板复用既有语义 token（primary / success / warning / danger / card-tool /
    role-agent-main / role-agent-sub / text-secondary）；色相允许跨类复用，识别
    主体是图标形状，颜色只作气质点缀。 */
import { type ReactNode } from 'react'
import {
  File,
  FileArchive,
  FileAudio,
  FileCode,
  FileCog,
  FileImage,
  FileJson,
  FileSpreadsheet,
  FileTerminal,
  FileText,
  FileType,
  FileVideo,
  Folder,
  Globe,
  Presentation,
} from 'lucide-react'
import { extOf } from './fileKind'

type IconSpec = { Icon: typeof File; color: string }

const CODE_EXTS = new Set([
  'py', 'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'go', 'rs', 'java', 'kt',
  'c', 'h', 'cpp', 'hpp', 'cc', 'cs', 'rb', 'php', 'vue', 'svelte',
  'dart', 'swift', 'scala', 'clj', 'lua', 'r', 'pl', 'sql', 'css', 'scss', 'less',
])
const SHELL_EXTS = new Set(['sh', 'bash', 'zsh', 'ps1', 'bat', 'cmd'])
const CONFIG_EXTS = new Set(['yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env'])
const DOC_EXTS = new Set(['docx', 'doc'])
const SHEET_EXTS = new Set(['xlsx', 'xls', 'csv', 'tsv'])
const SLIDE_EXTS = new Set(['pptx', 'ppt'])
const ARCHIVE_EXTS = new Set(['zip', 'tar', 'gz', 'tgz', '7z', 'rar', 'bz2', 'xz'])
const AUDIO_EXTS = new Set(['mp3', 'wav', 'flac', 'ogg', 'm4a', 'aac'])
const VIDEO_EXTS = new Set(['mp4', 'mov', 'webm', 'mkv', 'avi'])
const RASTER_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico'])
const MD_EXTS = new Set(['md', 'markdown', 'mdx'])

function specOf(ext: string): IconSpec {
  if (MD_EXTS.has(ext)) return { Icon: FileText, color: 'var(--color-primary)' }
  if (ext === 'pdf') return { Icon: FileText, color: 'var(--color-danger)' }
  if (DOC_EXTS.has(ext)) return { Icon: FileType, color: 'var(--color-role-agent-main)' }
  if (SHEET_EXTS.has(ext)) return { Icon: FileSpreadsheet, color: 'var(--color-success)' }
  if (SLIDE_EXTS.has(ext)) return { Icon: Presentation, color: 'var(--color-card-tool)' }
  if (ext === 'html' || ext === 'htm') return { Icon: Globe, color: 'var(--color-card-tool)' }
  if (ext === 'svg') return { Icon: FileImage, color: 'var(--color-role-agent-main)' }
  if (RASTER_EXTS.has(ext)) return { Icon: FileImage, color: 'var(--color-role-agent-sub)' }
  if (ext === 'json') return { Icon: FileJson, color: 'var(--color-warning)' }
  if (CODE_EXTS.has(ext)) return { Icon: FileCode, color: 'var(--color-role-agent-sub)' }
  if (SHELL_EXTS.has(ext)) return { Icon: FileTerminal, color: 'var(--color-success)' }
  if (CONFIG_EXTS.has(ext)) return { Icon: FileCog, color: 'var(--color-text-secondary)' }
  if (ARCHIVE_EXTS.has(ext)) return { Icon: FileArchive, color: 'var(--color-warning)' }
  if (AUDIO_EXTS.has(ext)) return { Icon: FileAudio, color: 'var(--color-role-agent-main)' }
  if (VIDEO_EXTS.has(ext)) return { Icon: FileVideo, color: 'var(--color-card-tool)' }
  if (ext === 'txt' || ext === 'log') return { Icon: FileText, color: 'var(--color-text-secondary)' }
  return { Icon: File, color: 'var(--color-text-tertiary)' }
}

/** 文件节点图标（按名称扩展名分派）。 */
export function fileTreeIcon(name: string): ReactNode {
  const { Icon, color } = specOf(extOf(name))
  return <Icon size={13} className="ws-tree-icon" style={{ color }} />
}

/** 目录节点图标（经典文件夹黄，与文件图标形成第一层区隔）。 */
export function dirTreeIcon(): ReactNode {
  return <Folder size={14} className="ws-tree-icon" style={{ color: 'var(--color-warning)' }} />
}
