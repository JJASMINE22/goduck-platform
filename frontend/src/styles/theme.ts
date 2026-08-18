import { theme, type ThemeConfig } from 'antd'
import zhCN from 'antd/locale/zh_CN'

/* AntD 全局主题 token —— 与 styles/tokens.css 同源（准则：单一渲染入口，配色集中管理）。
   浅色算法 + Google 蓝主色；画布冷灰（与 --color-bg 同值）衬托纯白模块卡片：
   Sider/Header/卡片显式给白底，模块间靠留隙 + 背景对比分层。 */
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
      '"Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif',
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      siderBg: '#ffffff',
      bodyBg: '#eef1f6',
      /* 折叠触发器：antd 默认深藏青在白色圆角卡片中突兀，改浅灰融入卡片 */
      triggerBg: '#f6f8fb',
      triggerColor: '#5f6368',
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

export const antdLocale = zhCN
