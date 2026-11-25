/* 工作区文件预览 —— 按类型分派（plan §3.2-3）。
   - text/markdown → /file 取文本；md 走 Markdown 渲染，其余包代码块高亮
   - html → /file 取文本，沙箱 iframe（srcDoc）直接渲染，渲染/源码可切换；
     sandbox 不含 allow-same-origin → 不透明源，页面脚本无法触碰应用 cookie/存储
   - svg → /file 取文本，SvgFigure 净化 + svg-scope 主题化渲染（语义类才有着色）
   - image → <img>（下载直链）
   - pdf → <iframe>（下载直链，浏览器原生）
   - office(docx/xlsx/pptx/csv…) → /preview 取 markitdown Markdown 渲染；503 降级
   - binary/未知/失败 → 降级卡片（元信息 + 下载）
   任何预览失败都回退卡片，杜绝破图/空白。
   chrome 已入右侧浮层族：头部玻璃延续 + Drawer 头部节奏（index.css
   .ws-preview__bar / .ws-view__resizer::before），结构（浮层遮挡/拖宽/铺满）
   维持伴随层语义不动。 */
import { Alert, Button, Segmented, Skeleton, Space, Tooltip, Typography } from 'antd'
import { type ReactNode, useState } from 'react'
import { Download, FileText, Maximize2, Minimize2, X } from 'lucide-react'
import { Markdown } from '@/components/markdown/Markdown'
import { SvgFigure } from '@/components/markdown/SvgFigure'
import { useWorkspaceFile, useWorkspacePreview } from '@/hooks/useWorkspace'
import { workspaceApi } from '@/api/workspace'
import { ApiError } from '@/api/client'
import type { UUID } from '@/types'
import { classifyFile, formatSize } from './fileKind'

const { Text } = Typography

/* 头部条（chrome 入族，视觉规格见 index.css .ws-preview__bar）：
   玻璃延续 + Drawer header 节奏；左：文件名/体积/时间，右：视图切换、
   铺满、下载、关闭。铺满层复用同一头部（props 注入差异行为） */
function DownloadBar({
  sessionId,
  path,
  name,
  size,
  modifiedAt,
  extra,
  onClose,
}: {
  sessionId: UUID
  path: string
  name: string
  size?: number
  modifiedAt?: string | null
  extra?: ReactNode
  onClose?: () => void
}): ReactNode {
  return (
    <div className="ws-preview__bar">
      <Space size={4}>
        <FileText size={14} />
        <Text strong style={{ fontSize: 14 }}>{name}</Text>
        {size != null ? <Text type="secondary" style={{ fontSize: 12 }}>{formatSize(size)}</Text> : null}
        {modifiedAt ? <Text type="secondary" style={{ fontSize: 12 }}>{modifiedAt.slice(0, 19).replace('T', ' ')}</Text> : null}
      </Space>
      <Space size={4}>
        {extra}
        <Button
          size="small"
          icon={<Download size={13} />}
          onClick={() => workspaceApi.download(sessionId, path)}
        >
          下载
        </Button>
        {onClose ? (
          <Tooltip title="关闭预览">
            <Button size="small" type="text" icon={<X size={14} />} onClick={onClose} />
          </Tooltip>
        ) : null}
      </Space>
    </div>
  )
}

function FallbackCard({
  sessionId,
  path,
  name,
  size,
  modifiedAt,
  reason,
  onClose,
}: {
  sessionId: UUID
  path: string
  name: string
  size?: number
  modifiedAt?: string | null
  reason: string
  onClose?: () => void
}) {
  return (
    <div className="ws-preview__fallback">
      <DownloadBar sessionId={sessionId} path={path} name={name} size={size} modifiedAt={modifiedAt} onClose={onClose} />
      <div className="ws-preview__fallback-body">
        <FileText size={40} />
        <Text type="secondary">{reason}</Text>
        <Button
          type="primary"
          icon={<Download size={14} />}
          onClick={() => workspaceApi.download(sessionId, path)}
        >
          下载查看
        </Button>
      </div>
    </div>
  )
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : '预览失败'
}

export function WorkspaceFilePreview({
  sessionId,
  path,
  name,
  onClose,
  fullscreen = false,
  onToggleFullscreen,
}: {
  sessionId: UUID
  path: string
  name: string
  onClose?: () => void
  /** 铺满层变体（WorkspaceView 注入）：标题条显示「退出铺满」还原钮 */
  fullscreen?: boolean
  onToggleFullscreen?: () => void
}): ReactNode {
  const kind = classifyFile(name)
  const downloadHref = workspaceApi.downloadUrl(sessionId, path)
  const [htmlView, setHtmlView] = useState<'render' | 'source'>('render')

  // 铺满/还原切换钮（WorkspaceView 注入；与拖拽调宽并存，FallbackCard 无正文不提供）
  const expandToggle = onToggleFullscreen ? (
    <Tooltip title={fullscreen ? '退出铺满' : '铺满屏幕'}>
      <Button
        size="small"
        type="text"
        icon={fullscreen ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        onClick={onToggleFullscreen}
      />
    </Tooltip>
  ) : null

  // —— 文本类：取 /file ——
  const fileQ = useWorkspaceFile(
    sessionId,
    kind === 'text' || kind === 'markdown' || kind === 'html' || kind === 'svg' ? path : null,
  )
  // —— office 类：取 /preview（markitdown）——
  const previewQ = useWorkspacePreview(sessionId, kind === 'office' ? path : null)

  const meta = { size: fileQ.data?.size, modifiedAt: fileQ.data?.modified_at }

  // 通用顶部下载条 + body（flush=true：body 去内边距不滚动，由 iframe 自行滚动）
  const wrap = (
    body: ReactNode,
    size?: number,
    modifiedAt?: string | null,
    flush = false,
    extra?: ReactNode,
  ): ReactNode => (
    <div className="ws-preview">
      <DownloadBar
        sessionId={sessionId}
        path={path}
        name={name}
        size={size}
        modifiedAt={modifiedAt}
        extra={expandToggle ? (extra ? <>{extra}{expandToggle}</> : expandToggle) : extra}
        onClose={onClose}
      />
      <div className={flush ? 'ws-preview__body ws-preview__body--flush' : 'ws-preview__body'}>{body}</div>
    </div>
  )

  if (kind === 'image') {
    return wrap(<img src={downloadHref} alt={name} style={{ maxWidth: '100%' }} />)
  }

  if (kind === 'pdf') {
    return wrap(
      <iframe src={downloadHref} title={name} style={{ width: '100%', height: '100%', border: 0 }} />,
    )
  }

  if (kind === 'html') {
    if (fileQ.isLoading) return wrap(<Skeleton active paragraph={{ rows: 8 }} />)
    if (fileQ.isError) {
      return <FallbackCard sessionId={sessionId} path={path} name={name} reason={errMsg(fileQ.error)} onClose={onClose} />
    }
    const data = fileQ.data
    if (!data || !data.is_text || !data.content) {
      return (
        <FallbackCard
          sessionId={sessionId}
          path={path}
          name={name}
          size={data?.size}
          modifiedAt={data?.modified_at}
          reason="该文件为二进制，无法在线预览"
          onClose={onClose}
        />
      )
    }
    const truncMsg = data.truncated
      ? '文件超过预览上限已截断，渲染/源码不完整，请下载查看完整内容'
      : null
    const toggle = (
      <Segmented
        size="small"
        value={htmlView}
        onChange={(v) => setHtmlView(v as 'render' | 'source')}
        options={[
          { label: '渲染', value: 'render' },
          { label: '源码', value: 'source' },
        ]}
      />
    )
    if (htmlView === 'render') {
      return wrap(
        <div className="ws-preview__html">
          {truncMsg ? <Alert type="warning" showIcon message={truncMsg} style={{ flexShrink: 0 }} /> : null}
          {/* 沙箱渲染：不含 allow-same-origin → 不透明源，脚本不能读应用
              cookie/存储；CDN 外链（echarts 等）仍可加载。切换文件由外层
              key 重挂载，重渲染即重执行脚本。 */}
          <iframe
            className="ws-preview__frame"
            sandbox="allow-scripts allow-popups allow-forms allow-modals"
            srcDoc={data.content}
            title={name}
          />
        </div>,
        data.size,
        data.modified_at,
        true,
        toggle,
      )
    }
    return wrap(
      <>
        <Markdown>{`\`\`\`html\n${data.content}\n\`\`\``}</Markdown>
        {truncMsg ? <Alert type="warning" showIcon message={truncMsg} style={{ margin: '8px 0' }} /> : null}
      </>,
      data.size,
      data.modified_at,
      false,
      toggle,
    )
  }

  if (kind === 'svg') {
    if (fileQ.isLoading) return wrap(<Skeleton active paragraph={{ rows: 8 }} />)
    if (fileQ.isError) {
      return <FallbackCard sessionId={sessionId} path={path} name={name} reason={errMsg(fileQ.error)} onClose={onClose} />
    }
    const data = fileQ.data
    if (!data || !data.is_text || !data.content) {
      return (
        <FallbackCard
          sessionId={sessionId}
          path={path}
          name={name}
          size={data?.size}
          modifiedAt={data?.modified_at}
          reason="该文件为二进制，无法在线预览"
          onClose={onClose}
        />
      )
    }
    // 复用聊天内联 SVG 渲染管线：DOMPurify 净化 + svg-scope 语义类主题化
    //（深浅色随平台）+ 硬编码色归一化；净化失败组件自身降级为源码展示。
    // 截断的半截 SVG XML 解析必失败 → 同样落源码展示，配合提示下载查看。
    return wrap(
      <>
        <SvgFigure code={data.content} />
        {data.truncated ? (
          <Alert
            type="info"
            showIcon
            message="内容较长已截断，图表可能不完整，请下载查看完整文件"
            style={{ margin: '8px 0' }}
          />
        ) : null}
      </>,
      data.size,
      data.modified_at,
    )
  }

  if (kind === 'text' || kind === 'markdown') {
    if (fileQ.isLoading) return wrap(<Skeleton active paragraph={{ rows: 8 }} />)
    if (fileQ.isError) {
      return <FallbackCard sessionId={sessionId} path={path} name={name} reason={errMsg(fileQ.error)} onClose={onClose} />
    }
    const data = fileQ.data
    if (!data || !data.is_text || !data.content) {
      return (
        <FallbackCard
          sessionId={sessionId}
          path={path}
          name={name}
          size={data?.size}
          modifiedAt={data?.modified_at}
          reason="该文件为二进制，无法在线预览"
          onClose={onClose}
        />
      )
    }
    return wrap(
      <>
        {kind === 'markdown' ? (
          <Markdown>{data.content}</Markdown>
        ) : (
          // 非 markdown 文本：包成代码块交给 Markdown 的 CodeBlock（高亮 + 复制）。
          <Markdown>{`\`\`\`${guessLang(name)}\n${data.content}\n\`\`\``}</Markdown>
        )}
        {data.truncated ? (
          <Alert
            type="info"
            showIcon
            message="内容较长已截断，请下载查看完整文件"
            style={{ margin: '8px 0' }}
          />
        ) : null}
      </>,
      data.size,
      data.modified_at,
    )
  }

  if (kind === 'office') {
    if (previewQ.isLoading) return wrap(<Skeleton active paragraph={{ rows: 8 }} />)
    if (previewQ.isError) {
      // 503（markitdown 未就绪）/ 415 / 其它 → 降级卡片
      return (
        <FallbackCard
          sessionId={sessionId}
          path={path}
          name={name}
          reason={errMsg(previewQ.error)}
          onClose={onClose}
        />
      )
    }
    if (previewQ.data) {
      return wrap(
        previewQ.data.markdown.trim() ? (
          <Markdown>{previewQ.data.markdown}</Markdown>
        ) : (
          <Text type="secondary">文档内容为空</Text>
        ),
      )
    }
  }

  // binary / 未知类型
  return <FallbackCard sessionId={sessionId} path={path} name={name} {...meta} reason="该类型不支持在线预览" onClose={onClose} />
}

/** 文件名 → 代码块语言提示（rehype-highlight 用；未命中走纯文本）。 */
function guessLang(filename: string): string {
  const ext = filename.toLowerCase().split('.').pop() ?? ''
  const map: Record<string, string> = {
    py: 'python', ts: 'typescript', tsx: 'tsx', js: 'javascript', jsx: 'jsx',
    json: 'json', yaml: 'yaml', yml: 'yaml', xml: 'xml', html: 'html', css: 'css',
    sql: 'sql', sh: 'bash', bash: 'bash', go: 'go', rs: 'rust', java: 'java',
    c: 'c', cpp: 'cpp', cs: 'csharp', rb: 'ruby', php: 'php', toml: 'ini',
  }
  return map[ext] ?? ''
}
