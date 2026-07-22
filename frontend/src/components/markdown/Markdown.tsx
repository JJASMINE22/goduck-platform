/* Markdown 渲染 —— agent 回复 / rubric / 附件清单展示。
   react-markdown + gfm + 代码高亮。代码块用等宽字体 + 浅底色（不引 hljs 主题 css，保持轻量）。 */
import { type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'

export function Markdown({ children }: { children: string }): ReactNode {
  return (
    <div className="gd-md" style={{ lineHeight: 1.7, wordBreak: 'break-word' }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          pre: (props) => (
            <pre
              {...props}
              style={{
                background: 'var(--color-border-soft)',
                padding: 12,
                borderRadius: 8,
                overflow: 'auto',
                fontSize: 12.5,
              }}
            />
          ),
          code: (props) => (
            <code {...props} style={{ fontFamily: 'var(--font-mono)', fontSize: '0.92em' }} />
          ),
          table: (props) => (
            <table {...props} style={{ borderCollapse: 'collapse', width: '100%', margin: '8px 0' }} />
          ),
          th: (props) => (
            <th {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px', background: 'var(--color-border-soft)' }} />
          ),
          td: (props) => <td {...props} style={{ border: '1px solid var(--color-border)', padding: '4px 8px' }} />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
