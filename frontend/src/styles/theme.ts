/* AntD 全局主题 token —— 与 styles/tokens.css 同源（准则：单一渲染入口，配色集中管理）。
   Light：defaultAlgorithm + Google 蓝主色；画布冷灰衬托纯白模块卡片。
   Dark：darkAlgorithm + 提亮蓝（Google 暗色系，与 tokens.css [data-theme=dark] 同值）；
   画布/抬升面/边框全部对齐 CSS 变量暗色值，玻璃外壳（Sider/Header）仍由
   .app-glass 的透明 token 接管。App.tsx 读 themeStore 动态取用。 */
import { theme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'

const FONT_FAMILY =
  '"Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif'

const lightTheme: ThemeConfig = {
  algorithm: theme.defaultAlgorithm,
  token: {
    colorPrimary: '#1a73e8',
    colorSuccess: '#1e8e3e',
    colorWarning: '#f9ab00',
    colorError: '#d93025',
    colorInfo: '#1a73e8',
    borderRadius: 10,
    colorBgBase: '#ffffff',
    fontFamily: FONT_FAMILY,
  },
  components: {
    Layout: {
      /* 侧栏/顶栏玻璃化（index.css .app-shell .app-glass）：token 层不再给实白底
         （组件 token 编译为内联 CSS 变量，优先级高于任何类选择器，是此前
         "玻璃贴白底"的根因）。透明让 .app-glass 的 rgba 半透 + backdrop-filter 生效 */
      headerBg: 'transparent',
      siderBg: 'transparent',
      bodyBg: '#eef1f6',
    },
    Menu: {
      itemHeight: 44,
      fontSize: 15,
    },
    Card: {
      borderRadiusLG: 14,
      colorBorderSecondary: '#e4e8f0',
    },
  },
}

const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorPrimary: '#8ab4f8',
    colorSuccess: '#57c985',
    colorWarning: '#fdd663',
    colorError: '#f28b82',
    colorInfo: '#8ab4f8',
    borderRadius: 10,
    colorBgBase: '#161c25',
    fontFamily: FONT_FAMILY,
  },
  components: {
    Layout: {
      headerBg: 'transparent',
      siderBg: 'transparent',
      bodyBg: '#0f141b',
    },
    Menu: {
      itemHeight: 44,
      fontSize: 15,
    },
    Card: {
      borderRadiusLG: 14,
      colorBorderSecondary: '#2a323d',
    },
  },
}

export function createAntdTheme(dark: boolean): ThemeConfig {
  return dark ? darkTheme : lightTheme
}

export const antdLocale = zhCN
