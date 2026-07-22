import type { ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'

/* AntD 全局主题 token —— 与 styles/tokens.css 同源（准则：单一渲染入口，配色集中管理）。
   中文 locale；圆角/主色与 CSS 变量对齐。 */
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#1677ff',
    colorSuccess: '#52c41a',
    colorWarning: '#faad14',
    colorError: '#ff4d4f',
    colorInfo: '#1677ff',
    borderRadius: 8,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      siderBg: '#ffffff',
      bodyBg: '#f5f7fa',
    },
    Menu: {
      itemHeight: 40,
    },
    Card: {
      borderRadiusLG: 12,
    },
  },
}

export const antdLocale = zhCN
