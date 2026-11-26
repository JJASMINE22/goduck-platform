/* image_understand 工具卡纯函数（拆出组件文件，避免 react-refresh 规则：
   组件文件只导出组件——同 kbSearchTool.ts 纪律）。 */

/** 工具名匹配：经 mcp_server 注册后为 {server}__image_understand */
export function isImageUnderstandTool(name: string): boolean {
  return name.endsWith('__image_understand')
}

/** 解包 image_understand 工具结果（可能「JSON 字符串包 JSON」双重编码，
 *  同 kbSearchTool.parseKbSearchPayload 的解包语义；流式半截 JSON → null） */
export function parseImageUnderstandPayload(raw: string): unknown | null {
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
