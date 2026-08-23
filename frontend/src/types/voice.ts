/* 语音输入（WS /sessions/{id}/voice）帧协议类型 —— 对应后端 app/api/voice.py
   + VoiceStartFrame（models/schemas.py）+ EventType 扩展（streaming/events.py）。
   上行：start（按下快照）/ audio（PCM16 二进制）/ flush（松开）/ pong（心跳回包）/
   cancel（推理期中止语音发起的 astream）；下行：ping（心跳探测）/ stt_chunk /
   stt_output / stt_empty + agent 事件（与 SSE 同名同构）。 */
import type { SSEEventType } from './stream'
import type { UUID } from './common'

/** 麦克风小状态机（与 chat status 正交联动；推理闸门仍复用 chat status） */
export type VoiceState = 'idle' | 'recording' | 'transcribing'

/** 下行帧类型 = SSE 事件类型 ∪ 语音专用帧 */
export type VoiceDownEventType = SSEEventType | 'ping' | 'stt_chunk' | 'stt_output' | 'stt_empty'

/** 下行帧壳：与 SSEEvent 同构（ping/stt_* 仅 WS 使用） */
export interface VoiceDownFrame<P = Record<string, unknown>> {
  type: VoiceDownEventType
  agent_path?: string | null
  payload: P
}

/** stt_chunk / stt_output payload */
export interface SttTranscriptPayload {
  transcript: string
}

/** start 帧携带的附件引用（与 SendMessageRequest.attachments 同构） */
export interface VoiceAttachment {
  upload_id: UUID
}

/** 上行 start 帧：按下麦克风时的快照（草稿/附件/KB/rubric） */
export interface VoiceStartFrame {
  type: 'start'
  /** 按压时输入框草稿快照；终稿非空时后端与之合并为一条消息（stt_output 下发合并稿） */
  draft?: string | null
  attachments: VoiceAttachment[]
  knowledge_base_ids: UUID[]
  rubric?: string | null
}

/** 上行控制帧（flush / pong / cancel） */
export type VoiceUpControlFrame =
  | { type: 'flush' }
  | { type: 'pong' }
  | { type: 'cancel' }
  | VoiceStartFrame

/** WS 连接状态（驱动麦克风按钮可按压性） */
export type VoiceConnState = 'connecting' | 'ready' | 'reconnecting'
