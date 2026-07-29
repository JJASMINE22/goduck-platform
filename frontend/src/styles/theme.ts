import { theme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'

/* AntD 全局主题 token —— 与 styles/tokens.css 同源（准则：单一渲染入口，配色集中管理）。
   浅色算法（Google AI Studio 风）+ Google 蓝主色；Layout/Card 显式给浅色底，与 CSS 变量对齐：
   页面浅灰底 #f8f9fa，Sider/Header/卡片纯白，靠细边框 + 软阴影分层。 */
export const antdTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#1a73e8',
    colorSuccess: '#1e8e3e',
    colorWarning: '#f9ab00',
    colorError: '#d93025',
    colorInfo: '#1a73e8',
    borderRadius: 10,
    colorBgBase: '#ffffff',
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      siderBg: '#ffffff',
      bodyBg: '#f8f9fa',
    },
    Menu: {
      itemHeight: 40,
    },
    Card: {
      borderRadiusLG: 14,
      colorBorderSecondary: '#e8eaed',
    },
  },
}

export const antdLocale = zhCN
