import { theme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'

/* AntD 全局主题 token —— 与 styles/tokens.css 同源（准则：单一渲染入口，配色集中管理）。
   暗色算法 + 电光青主色；Layout/Card 显式给暗色底，与 CSS 变量对齐。 */
export const antdTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: '#22d3ee',
    colorSuccess: '#34d399',
    colorWarning: '#fbbf24',
    colorError: '#fb7185',
    colorInfo: '#22d3ee',
    borderRadius: 6,
    colorBgBase: '#0b1020',
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: {
      headerBg: '#131a2b',
      siderBg: '#0f1626',
      bodyBg: '#0b1020',
    },
    Menu: {
      itemHeight: 40,
    },
    Card: {
      borderRadiusLG: 8,
      colorBorderSecondary: '#243049',
    },
  },
}

export const antdLocale = zhCN
