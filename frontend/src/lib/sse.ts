/* SSE 流式封装（DESIGN §8）。
   ⚠️ 后端 POST /sessions/{id}/messages 返回 SSE（sse-starlette），浏览器原生
   EventSource 仅支持 GET，必须用 fetch-based 方案（@microsoft/fetch-event-source）。
   POST 场景禁止自动重连（幂等性未知，避免重复发消息）—— onerror 抛错终止。
   GET 观察流（/sessions/{id}/stream）只读幂等：204=无 run → 'closed' 终止；
   resync（观察者落后超出缓冲）→ 'resync' 终止，由调用方重建后重挂。
   重试策略统一由调用方控制（useStreamChat 的有界重试循环），本层不自动重连。 */

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

export type StreamTermination =
  | 'done'
  | 'interrupt'
  | 'error'
  | 'canceled'
  | 'resync'
  | 'closed'

/** GET 观察流的 204（无 run）信号：非错误，静默映射为 'closed' */
class NoRunSignal extends Error {}

/** 后端拒绝（非 2xx，message 携带后端 detail 真实原因）。
 *  与网络层异常（fetch TypeError 等）区分：拒绝=请求未成立、无 run 启动，
 *  调用方应直接落错误并复位状态，而非走"连接中断截断"的网络异常语义。 */
export class SseRejectedError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'SseRejectedError'
    this.status = status
  }
}

/** 读取非 2xx 响应的 FastAPI detail（尽力而为，失败回落状态码文案） */
async function rejectionFrom(res: Response): Promise<SseRejectedError> {
  let message = `请求被拒绝（${res.status}）`
  try {
    const body = (await res.clone().json()) as { detail?: unknown } | null
    if (body && typeof body.detail === 'string' && body.detail) {
      message = body.detail
    }
  } catch {
    /* body 非 JSON（代理错误页等）：保留状态码文案 */
  }
  return new SseRejectedError(res.status, message)
}

/** 解析 SSE data 字段为 SSEEvent；非法行安全跳过 */
function parseEvent(rawEvent: string | null, rawData: string, rawId: string | null): SSEEvent | null {
  // 后端 to_sse：{event: <type>, data: <json>, id?: "{token}.{seq}"}。
  // fetch-event-source 拆出 ev.event/ev.data/ev.id，data 里也带 type。
  let parsed: SSEEvent
  try {
    parsed = JSON.parse(rawData) as SSEEvent
  } catch {
    return null
  }
  // 以 event 字段为权威类型（data.type 兜底）
  if (rawEvent) parsed.type = rawEvent as SSEEvent['type']
  if (rawId) parsed.id = rawId
  if (!parsed.payload) parsed.payload = {}
  return parsed
}

/**
 * 发起 POST-SSE 流，直到终止事件 / 流关闭 / 异常。
 * @returns 终止原因（done/interrupt/error/canceled/resync/closed）；网络异常则 throw。
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
        throw await rejectionFrom(res)
      },
      onmessage(ev) {
        const e = parseEvent(ev.event, ev.data, ev.id)
        if (!e) return
        handlers.onEvent(e)
        if (TERMINAL_EVENTS.includes(e.type) || e.type === 'resync') {
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

/**
 * GET 观察流（只读幂等）：重进会话 / 断连恢复的补帧通道。
 * @param lastEventId 上次收到的 SSE id（"{token}.{seq}"）——增量对齐水位；
 *                   缺省则全量回放当前 run（服务端语义）。
 * @returns 终止原因；204（无 run）→ 'closed'；网络异常则 throw。
 */
export async function streamGetSse(
  path: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<StreamTermination> {
  const url = `${ApiClient.BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  let termination: StreamTermination = 'closed'

  try {
    await fetchEventSource(url, {
      method: 'GET',
      headers: { Accept: 'text/event-stream' },
      signal,
      openWhenHidden: true,
      async onopen(res) {
        if (res.status === 204) throw new NoRunSignal() // 无 run（也无 TTL 内终态）
        if (res.ok && res.headers.get('content-type')?.includes('text/event-stream')) return
        throw await rejectionFrom(res)
      },
      onmessage(ev) {
        const e = parseEvent(ev.event, ev.data, ev.id)
        if (!e) return
        handlers.onEvent(e)
        if (TERMINAL_EVENTS.includes(e.type) || e.type === 'resync') {
          termination = e.type as StreamTermination
        }
      },
      onclose() {},
      onerror(err) {
        throw err
      },
    })
    return termination
  } catch (err) {
    if (err instanceof NoRunSignal) return 'closed'
    if (err instanceof DOMException && err.name === 'AbortError') return 'closed'
    handlers.onFatal?.(err)
    throw err
  }
}
