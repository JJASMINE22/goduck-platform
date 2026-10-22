/* 统一列表分页配置 —— 所有 Table/列表页复用（避免风格分叉）。
   背景：antd 默认翻页箭头（Outlined 双角括号）+ 无底选中态与项目
   lucide 线性图标 + 圆润软填充语言不符（设计走查反馈）。
   三层协作：本文件换图标与总数文案；index.css「统一分页风格」段做形状/配色
   （CSS 变量随明暗主题）；theme.ts 不参与（颜色全部走 tokens.css 同源变量）。
   Table 的 pagination 配置直接传本对象（TablePaginationConfig extends
   PaginationProps，itemRender 可用，antd 5.22 类型已核）。 */
import type { ReactNode } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import type { PaginationProps } from 'antd'

/** 翻页箭头换 lucide 单角括号（16px，与全站线性图标同粗细同语言）；
    页码与跳页省略号保留 antd 原生渲染（文字/省略号无风格冲突）。 */
const itemRender: PaginationProps['itemRender'] = (
  _page: number,
  type: 'page' | 'prev' | 'next' | 'jump-prev' | 'jump-next',
  originalElement: ReactNode,
): ReactNode => {
  if (type === 'prev') return <ChevronLeft size={16} />
  if (type === 'next') return <ChevronRight size={16} />
  return originalElement
}

/** 列表页标准分页：每页 15 行 + 左侧总数（走查反馈：缺总数信息）。 */
export const listPager = {
  pageSize: 15,
  itemRender,
  showTotal: (total: number) => `共 ${total} 条`,
} as const
