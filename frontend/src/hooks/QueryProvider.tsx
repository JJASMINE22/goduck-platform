/* QueryClientProvider 组件 —— 仅导出组件（满足 react-refresh 规则）。
   QueryClient 实例懒创建于组件内 useState，全局单例。 */
import { QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { createQueryClient } from './queryKeys'

export function QueryProvider({ children }: { children: ReactNode }): ReactNode {
  const [client] = useState(() => createQueryClient())
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
