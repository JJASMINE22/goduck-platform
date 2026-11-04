/* 应用根：组装全局 Provider + 路由（DESIGN §9）。
   Provider 嵌套顺序（外→内）：ConfigProvider(主题/locale) → AntD App(message context)
     → QueryProvider → BrowserRouter → AppRoot(useApp 注入静态桥接) → Routes。
   登录体系：AppRoot 挂载时 /auth/me 探测一次（身份唯一真相 = userStore）；
   guest 访问业务路由由 RequireAuth 回登录页，管理员路由再加 RequireManager。 */
import { App as AntApp, ConfigProvider } from 'antd'
import { useEffect, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { antdLocale, createAntdTheme } from '@/styles/theme'
import { setStaticFns } from '@/lib/antdStatic'
import { QueryProvider } from '@/hooks/QueryProvider'
import { useThemeStore } from '@/store/themeStore'
import { RequireAuth, RequireManager } from '@/components/routing/guards'
import { AppShell } from '@/components/layout/AppShell'
import { LoginPage } from '@/pages/auth/LoginPage'
import { ProfilePage } from '@/pages/profile/ProfilePage'
import { AdminPage } from '@/pages/admin/AdminPage'
import { AgentsListPage } from '@/pages/agents/AgentsListPage'
import { AgentEditorPage } from '@/pages/agents/AgentEditorPage'
import { SessionsListPage } from '@/pages/sessions/SessionsListPage'
import { ChatPage } from '@/pages/sessions/ChatPage'
import { CronJobsPage } from '@/pages/cron/CronJobsPage'
import { ModelsPage } from '@/pages/settings/ModelsPage'
import { EmbeddingsPage } from '@/pages/settings/EmbeddingsPage'
import { McpPage } from '@/pages/settings/McpPage'
import { SkillsPage } from '@/pages/settings/SkillsPage'
import { KnowledgePage } from '@/pages/settings/KnowledgePage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { useCurrentUser } from '@/store/userStore'

/** 在 AntD App 内部取出 message/notification/modal 实例，注入静态桥接 */
function AppRoot(): ReactNode {
  const fns = AntApp.useApp()
  useEffect(() => setStaticFns(fns), [fns])

  const fetchMe = useCurrentUser((s) => s.fetchMe)
  useEffect(() => {
    void fetchMe()
  }, [fetchMe])

  return (
    <Routes>
      {/* 登录页公开（注册随登录页弹窗；欢迎过渡层内完成跳转） */}
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><AppShell /></RequireAuth>}>
        <Route index element={<Navigate to="/agents" replace />} />
        <Route path="/agents" element={<AgentsListPage />} />
        {/* 编辑器为管理员专属：非管理员仍可在列表页悬浮预览架构 */}
        <Route path="/agents/new" element={<RequireManager><AgentEditorPage /></RequireManager>} />
        <Route path="/agents/:id" element={<RequireManager><AgentEditorPage /></RequireManager>} />
        <Route path="/sessions" element={<SessionsListPage />} />
        <Route path="/sessions/:id" element={<ChatPage />} />
        <Route path="/cron" element={<CronJobsPage />} />
        <Route path="/settings/models" element={<ModelsPage />} />
        {/* 向量模型注册路由随 KB 能力（后端 kb_enabled=false 时 /embeddings 未挂载） */}
        <Route path="/settings/embeddings" element={<EmbeddingsPage />} />
        <Route path="/settings/mcp" element={<McpPage />} />
        <Route path="/settings/skills" element={<SkillsPage />} />
        <Route path="/settings/knowledge" element={<KnowledgePage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/admin" element={<RequireManager><AdminPage /></RequireManager>} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

export default function App(): ReactNode {
  const dark = useThemeStore((s) => s.theme === 'dark')
  return (
    <ConfigProvider theme={createAntdTheme(dark)} locale={antdLocale}>
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
