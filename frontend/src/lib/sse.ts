/* SSE 流式封装（DESIGN §8）。
   ⚠️ 后端 POST /sessions/{id}/messages 返回 SSE（sse-starlette），浏览器原生
   EventSource 仅支持 GET，必须用 fetch-based 方案（@microsoft/fetch-event-source）。
   POST 场景禁止自动重连（幂等性未知，避免重复发消息）—— onerror 抛错终止。 */

import { fetchEventSource } from '@microsoft/fetch-event-source'
import { ApiClient } from '@/api/client'
import { TERMINAL_EVENTS } from '@/types'
import type { SSEEvent } from '@/types'

export interface StreamHandlers {
  /** 每个解析出的 SSE 事件（含中间 + 终止） */
  onEvent: (e: SSEEvent) => void
  /** 网络层异常（fetch 失败 / 非 200 / 解析失败）—— 非后端 error 事件 */
  onFatal?: (err: unknown) => void
}

export type StreamTermination = 'done' | 'interrupt' | 'error' | 'closed'

/** 解析 SSE data 字段为 SSEEvent；非法行安全跳过 */
function parseEvent(rawEvent: string | null, rawData: string): SSEEvent | null {
  // 后端 to_sse：{event: <type>, data: <json>}。
  // fetch-event-source 拆出 ev.event=<type>、ev.data=<json>，data 里也带 type。
  let parsed: SSEEvent
  try {
    parsed = JSON.parse(rawData) as SSEEvent
  } catch {
    return null
  }
  // 以 event 字段为权威类型（data.type 兜底）
  if (rawEvent) parsed.type = rawEvent as SSEEvent['type']
  if (!parsed.payload) parsed.payload = {}
  return parsed
}

/**
 * 发起 POST-SSE 流，直到终止事件 / 流关闭 / 异常。
 * @returns 终止原因（done/interrupt/error/closed）；网络异常则 throw。
 */
export async function streamPostSse(
  path: string,
  body: unknown,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<StreamTermination> {
  const url = `${ApiClient.BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  let termination: StreamTermination = 'closed'

  try {
    await fetchEventSource(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal,
      openWhenHidden: true, // 切后台不断流
      // POST 幂等性未知：禁止自动重连（DESIGN §8.4）
      async onopen(res) {
        if (res.ok && res.headers.get('content-type')?.includes('text/event-stream')) return
        throw new Error(`SSE 连接异常（${res.status}）`)
      },
      onmessage(ev) {
        const e = parseEvent(ev.event, ev.data)
        if (!e) return
        handlers.onEvent(e)
        if (TERMINAL_EVENTS.includes(e.type)) {
          // 终止事件到达，记录原因；流会随后由后端关闭触发 onclose
          termination = e.type as StreamTermination
        }
      },
      onclose() {
        // 后端主动关闭（正常结束）。若尚未记录终止事件则视为 closed。
      },
      onerror(err) {
        // 网络层异常：抛错以停止重连，透传给外层 catch
        throw err
      },
    })
    return termination
  } catch (err) {
    // abort 不算 fatal
    if (err instanceof DOMException && err.name === 'AbortError') return 'closed'
    handlers.onFatal?.(err)
    throw err
  }
}
