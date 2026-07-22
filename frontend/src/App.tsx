/* 应用根：组装全局 Provider + 路由（DESIGN §9）。
   Provider 嵌套顺序（外→内）：ConfigProvider(主题/locale) → AntD App(message context)
     → QueryProvider → BrowserRouter → AppRoot(useApp 注入静态桥接) → Routes。 */
import { App as AntApp, ConfigProvider } from 'antd'
import { useEffect, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { antdLocale, antdTheme } from '@/styles/theme'
import { setStaticFns } from '@/lib/antdStatic'
import { QueryProvider } from '@/hooks/QueryProvider'
import { AppShell } from '@/components/layout/AppShell'
import { AgentsListPage } from '@/pages/agents/AgentsListPage'
import { AgentEditorPage } from '@/pages/agents/AgentEditorPage'
import { SessionsListPage } from '@/pages/sessions/SessionsListPage'
import { ChatPage } from '@/pages/sessions/ChatPage'
import { ModelsPage } from '@/pages/settings/ModelsPage'
import { McpPage } from '@/pages/settings/McpPage'
import { SkillsPage } from '@/pages/settings/SkillsPage'
import { NotFoundPage } from '@/pages/NotFoundPage'

/** 在 AntD App 内部取出 message/notification/modal 实例，注入静态桥接 */
function AppRoot(): ReactNode {
  const fns = AntApp.useApp()
  useEffect(() => setStaticFns(fns), [fns])

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/agents" replace />} />
        <Route path="/agents" element={<AgentsListPage />} />
        <Route path="/agents/new" element={<AgentEditorPage />} />
        <Route path="/agents/:id" element={<AgentEditorPage />} />
        <Route path="/sessions" element={<SessionsListPage />} />
        <Route path="/sessions/:id" element={<ChatPage />} />
        <Route path="/settings/models" element={<ModelsPage />} />
        <Route path="/settings/mcp" element={<McpPage />} />
        <Route path="/settings/skills" element={<SkillsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

export default function App(): ReactNode {
  return (
    <ConfigProvider theme={antdTheme} locale={antdLocale}>
      <AntApp>
        <QueryProvider>
          <BrowserRouter>
            <AppRoot />
          </BrowserRouter>
        </QueryProvider>
      </AntApp>
    </ConfigProvider>
  )
}
