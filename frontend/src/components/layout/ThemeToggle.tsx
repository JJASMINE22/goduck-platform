/* 主题切换（Light | Dark）—— 侧边栏卡片底部（用户区上方）。
    展开态：流体玻璃分段胶囊（Sun 浅色 / Moon 深色），选中项为玻璃凸起 + 主色，
    语言与侧栏菜单选中态一致；折叠态：单一圆形图标钮（图标示当前主题，点击切换）。
    切换即时生效：tokens.css 变量集 / AntD algorithm / mermaid 重渲染全链跟随。 */
import { Moon, Sun } from 'lucide-react'
import type { ReactNode } from 'react'
import { useThemeStore, type ThemeMode } from '@/store/themeStore'

const OPTIONS: Array<{ mode: ThemeMode; label: string; icon: ReactNode }> = [
  { mode: 'light', label: '浅色', icon: <Sun size={14} /> },
  { mode: 'dark', label: '深色', icon: <Moon size={14} /> },
]

export function ThemeToggle({ collapsed = false }: { collapsed?: boolean }): ReactNode {
  const theme = useThemeStore((s) => s.theme)
  const setTheme = useThemeStore((s) => s.setTheme)

  if (collapsed) {
    const dark = theme === 'dark'
    return (
      <button
        type="button"
        className="theme-toggle--icon"
        onClick={() => setTheme(dark ? 'light' : 'dark')}
        title={dark ? '切换到浅色模式' : '切换到深色模式'}
        aria-label={dark ? '切换到浅色模式' : '切换到深色模式'}
      >
        {dark ? <Sun size={16} /> : <Moon size={16} />}
      </button>
    )
  }

  return (
    <div className="theme-toggle" role="group" aria-label="页面风格">
      {OPTIONS.map((opt) => (
        <button
          key={opt.mode}
          type="button"
          className={`theme-toggle__opt${theme === opt.mode ? ' theme-toggle__opt--active' : ''}`}
          onClick={() => setTheme(opt.mode)}
          aria-pressed={theme === opt.mode}
        >
          {opt.icon}
          {opt.label}
        </button>
      ))}
    </div>
  )
}
