/* AntD 静态方法桥接 —— 让 QueryCache / api 层等非组件代码也能用 message/notification。
   AntD v5 推荐：App 组件包裹 → App.useApp() 拿实例 → 注入到这里。
   未注入前调用安全降级（no-op），避免初始化竞态报错。 */
import { App } from 'antd'

type StaticFns = ReturnType<typeof App.useApp>

let fns: StaticFns | null = null

/** 在根 AppProvider 内调用，注入 message/notification/modal 实例 */
export function setStaticFns(value: StaticFns): void {
  fns = value
}

/** 取当前注入的静态实例（可能为 null，调用方需可选链） */
export function getStaticFns(): StaticFns | null {
  return fns
}

/** 便捷：直接弹错误 toast（无实例时静默，不抛） */
export function toastError(content: string): void {
  fns?.message.error(content)
}

/** 便捷：直接弹成功 toast */
export function toastSuccess(content: string): void {
  fns?.message.success(content)
}
