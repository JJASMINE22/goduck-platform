/* API 客户端基础封装（DESIGN §6.1）。
   - BASE_URL 走环境变量，dev 由 Vite proxy /api -> 后端，禁止硬编码 localhost（准则：路径注入）
   - 统一 JSON / 纯文本 / multipart 三类请求
   - 错误归一为 ApiError，保留后端 detail（中文消息可直接展示，准则 F6：前端不信任、后端是安全） */

const BASE_URL = import.meta.env.VITE_API_BASE ?? '/api'

/** 归一化错误 —— 后端 HTTPException.detail 可能是 string 或 object（如删除被引用模型的 409） */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? deriveMessage(status, detail))
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function deriveMessage(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail.length > 0) return detail
  // FastAPI 422 校验错误聚合（detail 即 [{loc,msg,type}] 数组）
  if (Array.isArray(detail)) {
    return (detail as Array<Record<string, unknown>>)
      .map((e) => e?.msg ?? '')
      .filter(Boolean)
      .join('; ')
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as Record<string, unknown>
    // 结构化 detail（如删除被引用模型/MCP server 的 409 {message, referencing_agents}）
    if (typeof obj.message === 'string') return obj.message
  }
  const map: Record<number, string> = {
    400: '请求参数有误',
    401: '未授权',
    403: '无权操作',
    404: '资源不存在',
    409: '操作冲突',
    413: '文件过大',
    415: '不支持的文件类型',
    422: '请求校验失败',
    500: '服务器内部错误',
  }
  return map[status] ?? `请求失败（${status}）`
}

type Query = Record<string, string | number | boolean | undefined | null>

function buildUrl(path: string, query?: Query): string {
  const url = `${BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null) params.append(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get('content-type') ?? ''
  if (ct.includes('application/json')) return res.json()
  return res.text()
}

/** 核心请求函数：拼地址 → 发请求 → 解析 → 归一错误 */
async function request<T>(
  method: string,
  path: string,
  opts: { query?: Query; body?: unknown; headers?: Record<string, string> } = {},
): Promise<T> {
  const { query, body, headers } = opts
  const init: RequestInit = { method, headers: { ...headers } }

  if (body !== undefined) {
    if (body instanceof FormData) {
      init.body = body // 浏览器自动设 multipart boundary
    } else {
      ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), init)
  } catch (e) {
    throw new ApiError(0, null, `网络请求失败：${e instanceof Error ? e.message : '无法连接后端'}`)
  }

  if (!res.ok) {
    let detail: unknown = null
    try {
      const body = await parseBody(res)
      // FastAPI 错误统一包裹为 {"detail": ...}：解包一层保留真实 detail
      //（string / {message,...} 结构体 / 422 数组），调用方直接按 detail 语义消费
      detail =
        body && typeof body === 'object' && !Array.isArray(body) && 'detail' in body
          ? (body as { detail: unknown }).detail
          : body
    } catch {
      /* 忽略解析失败，保留 status */
    }
    // 401 全局拦截：会话失效（过期/停用/改密吊销）→ 整页跳登录（带回跳地址）。
    // /auth/* 自身（login/register/me）不跳，避免登录页报错被劫持成循环跳转。
    if (
      res.status === 401 &&
      !path.startsWith('/auth/') &&
      !window.location.pathname.startsWith('/login')
    ) {
      const from = encodeURIComponent(window.location.pathname + window.location.search)
      window.location.assign(`/login?from=${from}`)
    }
    throw new ApiError(res.status, detail)
  }

  // 204 / 空响应
  if (res.status === 204) return undefined as T
  return (await parseBody(res)) as T
}

// —— 语义化辅助（按域文件复用）——
export const get = <T>(path: string, query?: Query) => request<T>('GET', path, { query })
export const post = <T>(path: string, body?: unknown) => request<T>('POST', path, { body })
export const put = <T>(path: string, body?: unknown) => request<T>('PUT', path, { body })
export const del = <T>(path: string, query?: Query) => request<T>('DELETE', path, { query })

/** 纯文本响应（/topology.mmd、/export.yaml 是 PlainTextResponse）—— 不按 JSON 解析 */
export const fetchText = (path: string) => request<string>('GET', path)

/** multipart 上传（/skills、/sessions/{id}/uploads） */
export const postForm = <T>(path: string, form: FormData) =>
  request<T>('POST', path, { body: form })

export const ApiClient = { get, post, put, del, fetchText, postForm, BASE_URL }
