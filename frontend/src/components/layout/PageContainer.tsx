/* 通用页面容器 —— 页面主题（标题/副标题/动作）上提到顶部 Header 卡
   （经 pageHeaderStore 注册，Topbar 渲染），本组件只负责渲染纯内容卡。
   包装层零内边距：内容卡与 Header 卡左右同缘、底部贴合 Content 底边
   （与侧边栏卡对齐），模块间距由外壳 Layout gap 统一提供。 */
import { useEffect, type ReactNode } from 'react'
import { usePageHeaderStore } from '@/store/pageHeaderStore'

export function PageContainer({
  title,
  subtitle,
  extra,
  children,
}: {
  title: string
  subtitle?: string
  extra?: ReactNode
  children: ReactNode
}): ReactNode {
  const setHeader = usePageHeaderStore((s) => s.setHeader)

  // 每次渲染重注册：extra 中的按钮回调闭包保持新鲜；卸载置空归还 Header。
  // 路由切换时旧页 cleanup 与新页注册在同一批次提交，无空窗闪烁。
  useEffect(() => {
    setHeader({ title, subtitle, extra })
    return () => setHeader(null)
  })

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="page-body">{children}</div>
    </div>
  )
}
