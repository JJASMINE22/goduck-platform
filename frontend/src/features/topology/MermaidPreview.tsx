/* Mermaid 预览组件（DESIGN 准则 F7：单一渲染入口，只渲染后端产出的文本）。
   code / theme 变化时异步渲染到容器。空文本显示占位。
   theme 入依赖：Light|Dark 切换后 mermaid 以对应主题重渲染。 */
import { useEffect, useRef, type ReactNode } from 'react'
import { renderMermaidInto } from '@/lib/mermaid'
import { useThemeStore } from '@/store/themeStore'

export function MermaidPreview({ code }: { code: string | null | undefined }): ReactNode {
  const ref = useRef<HTMLDivElement>(null)
  const theme = useThemeStore((s) => s.theme)
  useEffect(() => {
    let active = true
    void (async () => {
      await renderMermaidInto(ref.current, code ?? '')
      // 防止卸载后写入
      if (!active && ref.current) ref.current.innerHTML = ''
    })()
    return () => {
      active = false
    }
  }, [code, theme])
  return <div ref={ref} style={{ overflow: 'auto' }} />
}
