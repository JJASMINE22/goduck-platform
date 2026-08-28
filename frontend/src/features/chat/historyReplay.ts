/* 历史回放重建 —— /history（checkpointer 真值）→ chatStore 视图模型。
   实时流的工具调用卡仅存于前端内存（SSE tool_call / tool_result 事件），
   刷新 / 重进 / 归档查看都会丢失；后端 _serialize_message 已在 AI 消息上携带
   tool_calls、在 ToolMessage 上携带 tool_call_id + content，这里把它们配对
   还原，使历史回放与实时流共享同一时间线视图（MessageList 无需感知数据来源）。

   时序约定（MessageList 按 createdAt 归并排序）：
   - 消息气泡 createdAt = 原始历史序数（tool 消息造成的空位无妨，只需单调）；
   - 工具卡 createdAt = 宿主 AI 消息序数 + 0.5：与实时流对齐——同一 LLM 消息的
     token 内容先流式到达，tool_call 事件在其后（updates 模式、消息完成时发出），
     工具卡因此稳定落在宿主气泡与下一条消息之间；
   - ToolMessage 不产生独立时间线条目，仅按 tool_call_id 回填宿主卡结果
     （与实时流的 setToolResult 同语义）。
   单调的序数时间戳同时保证 removeTurn 的回合时间窗过滤（createdAt ∈
   [turnStart, turnEnd)）对历史重建的工具卡同样成立——刷新后回撤仍能整段移除
   本回合的工具卡。 */
import type { ChatMessage, ToolCallEntry } from '@/store/chatStore'
import type { MessageOut } from '@/types'

/** 工具卡相对宿主 AI 消息的时序偏移：严格介于宿主（i）与下一条消息（i+1）之间 */
const TOOL_CARD_TS_OFFSET = 0.5

export interface HistoryReplay {
  messages: ChatMessage[]
  toolCalls: ToolCallEntry[]
}

/** 重建历史视图：human/ai → 气泡（空内容 AI 跳过，与实时流一致，避免空气泡）；
    ai.tool_calls → 工具卡；tool → 按 tool_call_id 回填结果。 */
export function buildHistoryReplay(history: MessageOut[]): HistoryReplay {
  const messages: ChatMessage[] = []
  const toolCalls: ToolCallEntry[] = []

  history.forEach((m, i) => {
    switch (m.role) {
      case 'human': {
        messages.push({
          id: m.id ?? `h${i}`,
          role: 'user',
          agentPath: 'root',
          content: typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
          // human 消息的附件缩略 + KB 徽标（后端从消息 additional_kwargs 查库回填），
          // 与实时发送时的快照形状一致 → 同一渲染路径
          attachments: m.attachments?.length ? m.attachments : undefined,
          kbs: m.kbs?.length ? m.kbs : undefined,
          createdAt: i,
        })
        break
      }
      case 'ai': {
        // 工具卡：caller 语义与实时流一致（msg.name ?? agent_path；
        // 历史无子代理命名空间，agent_path 恒为 root）
        for (const tc of Array.isArray(m.tool_calls) ? m.tool_calls : []) {
          const name = typeof tc.name === 'string' ? tc.name : 'unknown'
          toolCalls.push({
            // id 缺失时用确定性兜底（同一次重建内唯一，跨刷新稳定）
            id: typeof tc.id === 'string' ? tc.id : `${name}-${i}-${toolCalls.length}`,
            name,
            args: (tc.args as Record<string, unknown> | undefined) ?? {},
            caller: m.name ?? 'root',
            agentPath: 'root',
            // 未见 ToolMessage 前保持"执行中"：与实时流中断语义一致
            // （含 HITL 待审批的工具——它确实尚未返回）
            hasResult: false,
            createdAt: i + TOOL_CARD_TS_OFFSET,
          })
        }
        const content = typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
        if (content.trim() !== '') {
          messages.push({
            id: m.id ?? `h${i}`,
            role: 'agent',
            agentPath: 'root',
            content,
            createdAt: i,
          })
        }
        break
      }
      case 'tool': {
        // 结果回填：ToolMessage 在消息序列中恒晚于宿主 AI 消息，此处宿主卡已收集；
        // 宿主缺失的孤儿结果（异常数据）防御性跳过
        const card = m.tool_call_id ? toolCalls.find((t) => t.id === m.tool_call_id) : null
        if (card) {
          card.result = typeof m.content === 'string' ? m.content : JSON.stringify(m.content)
          card.hasResult = true
        }
        break
      }
      default:
        break // 其它角色（未知/自定义）不进时间线
    }
  })

  return { messages, toolCalls }
}
