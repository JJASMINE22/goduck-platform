/* 工作区文件预览 —— 按类型分派（plan §3.2-3）。
   - text/markdown → /file 取文本；md 走 Markdown 渲染，其余包代码块高亮
   - image → <img>（下载直链）
   - pdf → <iframe>（下载直链，浏览器原生）
   - office(docx/xlsx/pptx/csv…) → /preview 取 markitdown Markdown 渲染；503 降级
   - binary/未知/失败 → 降级卡片（元信息 + 下载）
   任何预览失败都回退卡片，杜绝破图/空白。 */
import { Alert, Button, Skeleton, Space, Typography } from 'antd'
import { type ReactNode } from 'react'
import { Download, FileText } from 'lucide-react'
import { Markdown } from '@/components/markdown/Markdown'
import { useWorkspaceFile, useWorkspacePreview } from '@/hooks/useWorkspace'
import { workspaceApi } from '@/api/workspace'
import { ApiError } from '@/api/client'
import type { UUID } from '@/types'
import { classifyFile, formatSize } from './fileKind'

const { Text } = Typography

function DownloadBar({
  sessionId,
  path,
  name,
  size,
  modifiedAt,
}: {
  sessionId: UUID
  path: string
  name: string
  size?: number
  modifiedAt?: string | null
}) {
  return (
    <div className="ws-preview__bar">
      <Space size={4}>
        <FileText size={14} />
        <Text strong style={{ fontSize: 13 }}>{name}</Text>
        {size != null ? <Text type="secondary" style={{ fontSize: 12 }}>{formatSize(size)}</Text> : null}
        {modifiedAt ? <Text type="secondary" style={{ fontSize: 12 }}>{modifiedAt.slice(0, 19).replace('T', ' ')}</Text> : null}
      </Space>
      <Button
        size="small"
        icon={<Download size={13} />}
        onClick={() => window.open(workspaceApi.downloadUrl(sessionId, path), '_blank')}
      >
        下载
      </Button>
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
}: {
  sessionId: UUID
  path: string
  name: string
  size?: number
  modifiedAt?: string | null
  reason: string
}) {
  return (
    <div className="ws-preview__fallback">
      <DownloadBar sessionId={sessionId} path={path} name={name} size={size} modifiedAt={modifiedAt} />
      <div className="ws-preview__fallback-body">
        <FileText size={40} />
        <Text type="secondary">{reason}</Text>
        <Button
          type="primary"
          icon={<Download size={14} />}
          onClick={() => window.open(workspaceApi.downloadUrl(sessionId, path), '_blank')}
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
}: {
  sessionId: UUID
  path: string
  name: string
}): ReactNode {
  const kind = classifyFile(name)
  const downloadHref = workspaceApi.downloadUrl(sessionId, path)

  // —— 文本类：取 /file ——
  const fileQ = useWorkspaceFile(sessionId, kind === 'text' || kind === 'markdown' ? path : null)
  // —— office 类：取 /preview（markitdown）——
  const previewQ = useWorkspacePreview(sessionId, kind === 'office' ? path : null)

  const meta = { size: fileQ.data?.size, modifiedAt: fileQ.data?.modified_at }

  // 通用顶部下载条 + body
  const wrap = (body: ReactNode, size?: number, modifiedAt?: string | null): ReactNode => (
    <div className="ws-preview">
      <DownloadBar sessionId={sessionId} path={path} name={name} size={size} modifiedAt={modifiedAt} />
      <div className="ws-preview__body">{body}</div>
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

  if (kind === 'text' || kind === 'markdown') {
    if (fileQ.isLoading) return wrap(<Skeleton active paragraph={{ rows: 8 }} />)
    if (fileQ.isError) {
      return <FallbackCard sessionId={sessionId} path={path} name={name} reason={errMsg(fileQ.error)} />
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
  return <FallbackCard sessionId={sessionId} path={path} name={name} {...meta} reason="该类型不支持在线预览" />
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
