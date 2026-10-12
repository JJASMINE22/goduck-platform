/* kb_search 工具卡纯函数（拆出组件文件，避免 react-refresh 规则：
   组件文件只导出组件——同 queryKeys.ts 纪律）。 */

/** 工具名匹配：经 mcp_server 注册后为 {server}__kb_search */
export function isKbSearchTool(name: string): boolean {
  return name.endsWith('__kb_search')
}

/** 解包 kb_search 工具结果（可能「JSON 字符串包 JSON」双重编码，
 *  同 ToolCallCard.formatPayload 的解包语义；流式半截 JSON → null） */
export function parseKbSearchPayload(raw: string): unknown | null {
  const s = raw.trim()
  if (!s) return null
  try {
    let parsed: unknown = JSON.parse(s)
    for (let i = 0; i < 3 && typeof parsed === 'string'; i++) parsed = JSON.parse(parsed)
    if (typeof parsed === 'object' && parsed !== null && 'result' in parsed) return parsed
  } catch {
    /* 半截 JSON（流式中）→ null */
  }
  return null
}
