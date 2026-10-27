/* 流式渲染上下文：Markdown 根节点下发，mermaid/svg 专项块消费——
   区分「流式增长中」与「终态」，驱动图表渐进渲染的占位文案切换。 */
import { createContext } from 'react'

export const MdStreamingContext = createContext(false)
