/* Mermaid 预览组件（DESIGN 准则 F7：单一渲染入口，只渲染后端产出的文本）。
   code 变化时异步渲染到容器。空文本显示占位。 */
import { useEffect, useRef, type ReactNode } from 'react'
import { renderMermaidInto } from '@/lib/mermaid'

export function MermaidPreview({ code }: { code: string | null | undefined }): ReactNode {
  const ref = useRef<HTMLDivElement>(null)
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
  }, [code])
  return <div ref={ref} style={{ overflow: 'auto' }} />
}
