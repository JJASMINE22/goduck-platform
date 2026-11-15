/* 媒体查询断点 hook —— 竖屏/窄视口自适应的 JS 侧触发源（CSS 侧见 index.css
   「窄视口自适应」一节，两处断点值须同步修改）。
   设计约定（详见 DESIGN.md §11）：
   - 布局分级以「视口/容器宽度」为唯一信号，不用 orientation——旋转显示器上的
     窄窗口、平板竖屏、桌面半屏窗口对布局的需求同构，宽度断点一次覆盖；
   - 内容区优先用容器查询（如 TurnRail），仅组件树必须切换时（侧栏抽屉化、
     编辑器抽屉形态）才经本 hook 在 JS 侧换树；
   - 纯展示降级（间距/字号/溢出策略）一律走 CSS，不进组件状态。
   实现走 useSyncExternalStore：matchMedia 是外部存储，快照/订阅/清理语义
   原生匹配，无 effect 内 setState，并发渲染下无撕裂。 */
import { useCallback, useMemo, useSyncExternalStore } from 'react'

/** 与 index.css 窄视口自适应段同源的断点定义 */
export const BREAKPOINTS = {
  /** 窄视口（平板竖屏 / 窄桌面窗口 ≤1100px）：侧栏自动折叠、编辑器改抽屉形态 */
  narrow: '(max-width: 1100px)',
  /** 紧凑视口（手机竖屏 ≤768px）：侧栏改抽屉、间距收紧、表格横向滚动 */
  compact: '(max-width: 768px)',
} as const

/** 订阅一条媒体查询的匹配态；查询串变化时自动重挂监听。 */
export function useMediaQuery(query: string): boolean {
  // matchMedia 句柄按 query 记忆（query 变化时重建）；环境不支持时回落 false
  const mql = useMemo(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia === 'undefined') return null
    try {
      return window.matchMedia(query)
    } catch {
      // 非法查询串等：回落为「永不匹配」
      return null
    }
  }, [query])

  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      if (!mql) return () => {}
      mql.addEventListener('change', onStoreChange)
      return () => mql.removeEventListener('change', onStoreChange)
    },
    [mql],
  )
  const getSnapshot = useCallback(() => mql?.matches ?? false, [mql])

  return useSyncExternalStore(subscribe, getSnapshot, () => false)
}
