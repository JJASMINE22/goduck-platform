/* 页面风格主题（Light | Dark）—— 全局唯一响应式主题源，持久化到 localStorage。
    切换三联动：
    1) <html data-theme="light|dark">：驱动 tokens.css 变量集切换（画布/玻璃/文本全套）；
    2) <html style.colorScheme>：浏览器原生控件（滚动条/表单）跟随；
    3) AntD ConfigProvider（App.tsx 读本 store 动态切 darkAlgorithm）。
    mermaid 渲染在 lib/mermaid.ts 内直接读取 data-theme（lib 不依赖 store）。 */
import { create } from 'zustand'

export type ThemeMode = 'light' | 'dark'

const THEME_KEY = 'goduck.theme'

function initialTheme(): ThemeMode {
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function applyTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode
  document.documentElement.style.colorScheme = mode
}

const init = initialTheme()
applyTheme(init)

interface ThemeState {
  theme: ThemeMode
  setTheme: (mode: ThemeMode) => void
  toggleTheme: () => void
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: init,
  setTheme: (mode) => {
    try {
      localStorage.setItem(THEME_KEY, mode)
    } catch {
      /* 隐私模式等存储不可用：仅本次会话生效 */
    }
    applyTheme(mode)
    set({ theme: mode })
  },
  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),
}))
