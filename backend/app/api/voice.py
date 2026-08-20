"""语音输入 WS 端点（plan：单 WS 全管线）。

``WS /sessions/{session_id}/voice``：一个连接承载完整语音管线
``浏览器音频 ⇄ 后端 ⇄ AssemblyAI(STT)``。连接**常驻**（会话页挂载即建、
跨回合复用、应用层心跳保活），语音回合按需懒建供应商连接。

回合协议（外层回合循环，done/stt_empty 后不关连接，回到空闲等 start）：
- 上行 start（按下）：附件/KB/rubric 快照 → 镜像 POST 校验（错误在录音前暴露）；
- 上行 audio（按住期间）：PCM16 帧原样转发供应商（懒连接）；
- 上行 flush（松开）：Terminate 供应商 → 冲刷终稿；
- 下行 stt_chunk/stt_output/stt_empty：转写事件直通；
- 终稿非空 → 镜像 POST 组装（附件/KB 锚点）→ agent.astream（同 thread_id 记忆
  续接）→ agent 事件经 WS 下行隧道（与 SSE 事件同名同构，前端复用 dispatch）。

守卫：
- 空转写（未发音/纯呼吸声/flush 超时）→ stt_empty 帧，不进 astream；
- StreamTracker.acquire/release 包住 astream —— 与 POST/withdraw 同闸防竞态，
  断连 finally 兜底 release；
- 空闲 10min 服务端 close(4408) 回收；供应商错误 → error 帧、回合作废、连接保持。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.api.stream import _build_agent_or_409
from app.deps import SimpleUser, get_ws_appstate
from app.models.schemas import VoiceStartFrame
from app.services.message_content import (
    MessageContentError,
    build_message_content,
    validate_message_content,
)
from app.services.session_service import SessionService
from app.services.stt.assemblyai_stt import AssemblyAISTT, STTError
from app.streaming.events import SSEEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["语音输入"])

# 供应商 WS 每回合懒建懒关；浏览器 WS 常驻复用（两级连接职责分离）
_CLOSE_SESSION_GONE = 4404
_CLOSE_AGENT_CONFLICT = 4409
_CLOSE_IDLE_TIMEOUT = 4408


class _SessionClosed(Exception):
    """浏览器 WS 已断开/发送失败，回合应立即终止。"""


class _VoiceSession:
    """一次浏览器 WS 连接的会话状态（连接常驻，跨多个语音回合）。"""

    def __init__(self, ws: WebSocket, st, settings):
        self.ws = ws
        self.st = st
        self.settings = settings
        self.last_inbound = time.monotonic()
        self.last_outbound = time.monotonic()
        self.closed = False
        # agent 推理期置位：空闲回收跳过（推理可长于 idle_timeout，且期间无上行），
        # ping 照发维持反代链路
        self.busy = False

    # ------------------------------------------------------------------ 收发
    async def send_json(self, ev: SSEEvent) -> None:
        """下行 JSON 帧；更新出站活性（心跳据此免发）。发送失败视为会话终结。"""
        if self.closed:
            raise _SessionClosed()
        try:
            await self.ws.send_text(ev.model_dump_json())
        except Exception as e:
            raise _SessionClosed() from e
        self.last_outbound = time.monotonic()

    async def send_raw(self, text: str) -> None:
        """下行原始 JSON 文本（ping 等协议帧，非 SSEEvent 模型）。"""
        if self.closed:
            raise _SessionClosed()
        try:
            await self.ws.send_text(text)
        except Exception as e:
            raise _SessionClosed() from e
        self.last_outbound = time.monotonic()

    async def send_error(self, message: str) -> None:
        """error 帧：回合作废但连接保持（前端复位麦克风并展示错误）。"""
        try:
            await self.send_json(SSEEvent(type="error", payload={"message": message}))
        except _SessionClosed:
            pass

    async def receive(self) -> dict:
        """原始 ASGI 帧接收；断连抛 WebSocketDisconnect，入站活性同步更新。"""
        msg = await self.ws.receive()
        self.last_inbound = time.monotonic()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1005))
        return msg

    # ------------------------------------------------------------------ 心跳
    async def heartbeat_loop(self, interval: int, idle_timeout: int) -> None:
        """空闲期应用层 ping（穿透所有代理）；入站静默超 idle_timeout 主动回收。

        出站 token/stt 帧本身就是活性（经 send_json 刷新 last_outbound），只有
        静默期才补 ping；存活判定看入站（客户端必须回 pong）；推理期（busy）
        不做空闲回收——期间前端不上行，仅靠出站流证明连接活着。
        """
        while not self.closed:
            await asyncio.sleep(interval)
            if self.closed:
                return
            now = time.monotonic()
            if not self.busy and now - self.last_inbound > idle_timeout:
                logger.info("voice ws idle %.0fs, recycling", now - self.last_inbound)
                await self.close(_CLOSE_IDLE_TIMEOUT)
                return
            if now - self.last_outbound >= interval:
                try:
                    # ping 为协议帧而非 SSEEvent（EventType 不含它），走 raw 通道
                    await self.send_raw(json.dumps({"type": "ping"}))
                except _SessionClosed:
                    return

    async def close(self, code: int) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code)
        except Exception:
            pass


async def _load_session(st, session_id: uuid.UUID) -> tuple[dict, SessionService]:
    """会话加载 + 归档校验（对齐 _load_session_or_404 语义，WS 侧转 close 码）。"""
    svc = SessionService(st.pool)
    session = await svc.get(session_id)
    if session is None:
        raise KeyError("session not found")
    if session["status"] == "archived":
        raise PermissionError("session archived")
    return session, svc


async def _await_start_frame(sess: _VoiceSession) -> VoiceStartFrame:
    """阻塞等待下一个 start 帧；跳过心跳 pong 等无关帧。

    无效 start（pydantic 校验失败）→ error 帧、继续等待；空闲期二进制帧丢弃
    （录音必须在 start 之后）。断连抛 WebSocketDisconnect。
    """
    while True:
        msg = await sess.receive()
        text = msg.get("text")
        if text is None:
            continue
        try:
            ctrl = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(ctrl, dict):
            continue
        if ctrl.get("type") != "start":
            # pong（心跳回包）/ 未知控制帧：活性已由 receive 记录，忽略
            continue
        try:
            return VoiceStartFrame.model_validate(ctrl)
        except Exception as e:
            await sess.send_error(f"无效的 start 帧: {e}")


async def _run_stt_round(
    sess: _VoiceSession, stt: AssemblyAISTT
) -> tuple[str, list[str], dict | None]:
    """执行一轮录音转写直至 flush/断连/供应商收尾。

    生产者-消费者并发：pump 读客户端帧转发音频（懒连接），consumer 归一化
    供应商事件并下行（格式化终句也按 stt_chunk 渲染为已确认中间稿；合并终稿
    由调用方在 flush 后单帧下发）。供应商错误标记 turn_error 但不中断回合
    （终止权威 = flush/断连），期间音频丢弃、error 帧即时下发。

    回合中收到新的 start（客户端在 error 复位后立即再按）→ 本轮作废，start
    帧原样交还（返回第三项），由回合循环直接开启新回合——按键永不丢失。

    返回 (final_transcript 合并文本, turn_errors, 超前 start 帧或 None)；
    断连抛 WebSocketDisconnect。
    """
    finals: list[str] = []
    errors: list[str] = []
    turn_error = False
    superseded_start: dict | None = None

    async def pump() -> None:
        """读客户端帧：flush 结束本回合；新 start 超越本轮；音频转发供应商。"""
        nonlocal turn_error, superseded_start
        while True:
            msg = await sess.receive()
            text = msg.get("text")
            if text is not None:
                try:
                    ctrl = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if not isinstance(ctrl, dict):
                    continue
                ctype = ctrl.get("type")
                if ctype == "flush":
                    await stt.terminate()
                    return
                if ctype == "start":
                    superseded_start = ctrl
                    return
                # pong / 其它控制帧：活性已由 receive 记录，忽略
            elif msg.get("bytes") is not None:
                if turn_error:
                    continue
                try:
                    await stt.send_audio(msg["bytes"])
                except (STTError, OSError) as e:
                    # 供应商连接失败/中断：回合仍由 flush 收口，后续音频丢弃
                    turn_error = True
                    errors.append(f"语音转写服务异常: {e}")
                    await sess.send_error(errors[-1])

    async def consume() -> None:
        """供应商事件 → 下行帧。全部按 stt_chunk 渲染（格式化终句同样是"已确认
        中间稿"）；完整终稿在 flush 后由端点合并为单条 stt_output 下发——前端据此
        一次性固化用户消息，多句长按天然合成一条消息。"""
        async for ev in stt.receive_events():
            await sess.send_json(
                SSEEvent(type="stt_chunk", payload={"transcript": ev.transcript})
            )
            if ev.type == "stt_output":
                finals.append(ev.transcript)

    consumer = asyncio.create_task(consume())
    try:
        await pump()  # 阻塞至 flush / 超越 start（或断连抛 WebSocketDisconnect）
        if superseded_start is not None:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            return "", errors, superseded_start
        # flush 已发 Terminate：receive_events 内部启用 flush_timeout 兜底收尾
        await consumer
    except BaseException:
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        raise
    return " ".join(part for part in finals if part), errors, None


async def _run_agent_round(
    sess: _VoiceSession,
    session: dict,
    agent,
    config: dict,
    start: VoiceStartFrame,
    transcript: str,
) -> None:
    """镜像 POST /messages：组装 content → astream → WS 下行隧道（tracker 同闸）。"""
    st = sess.st
    built = await build_message_content(
        st,
        session,
        user_query=transcript,
        attachments=start.attachments,
        knowledge_base_ids=start.knowledge_base_ids,
    )

    svc = SessionService(st.pool)
    thread_id = session["thread_id"]
    st.stream_tracker.acquire(thread_id)
    try:
        async for ev in st.stream_adapter.stream_message(
            agent,
            built.content,
            config,
            start.rubric,
            upload_ids=built.upload_ids,
            raw_content=transcript,
        ):
            # done/interrupt/error 终态事件由 adapter 产出，前端据其落态
            await sess.send_json(ev)
    finally:
        st.stream_tracker.release(thread_id)
        await svc.touch(session["id"])


async def _session_gone(sess: _VoiceSession, code: int, message: str) -> None:
    """校验失败：error 帧 + 立即 close（连接级错误，非回合级）。"""
    await sess.send_error(message)
    await sess.close(code)


@router.websocket("/{session_id}/voice")
async def voice_ws(ws: WebSocket, session_id: uuid.UUID):
    settings = ws.app.state.appstate.settings
    st = get_ws_appstate(ws)
    await ws.accept()
    sess = _VoiceSession(ws, st, settings)

    # 连接级校验：会话存在/未归档、智能体可构建（失败 → error 帧 + close）
    try:
        session, _svc = await _load_session(st, session_id)
    except KeyError:
        await _session_gone(sess, _CLOSE_SESSION_GONE, "session not found")
        return
    except PermissionError as e:
        await _session_gone(sess, _CLOSE_SESSION_GONE, str(e))
        return
    try:
        agent = await _build_agent_or_409(st, session["agent_id"])
    except HTTPException as e:
        await _session_gone(sess, _CLOSE_AGENT_CONFLICT, str(e.detail))
        return

    config = {
        "configurable": {
            "thread_id": session["thread_id"],
            "langgraph_auth_user": SimpleUser(session["user_id"]),
        }
    }

    heartbeat = asyncio.create_task(
        sess.heartbeat_loop(
            interval=settings.voice_heartbeat_interval,
            idle_timeout=settings.voice_idle_timeout,
        )
    )
    logger.info("voice ws open: session=%s", session_id)

    try:
        pending_start: dict | None = None
        while True:  # —— 回合循环（连接常驻）——
            if pending_start is not None:
                # 上一轮被新 start 超越：直接开启新回合（按键不丢失）
                try:
                    start = VoiceStartFrame.model_validate(pending_start)
                except Exception as e:  # noqa: BLE001
                    await sess.send_error(f"无效的 start 帧: {e}")
                    pending_start = None
                    continue
                pending_start = None
            else:
                start = await _await_start_frame(sess)

            # start 快照校验（录音前暴露配置错误；失败 → error 帧、回合作废）
            try:
                await validate_message_content(
                    st, session,
                    attachments=start.attachments,
                    knowledge_base_ids=start.knowledge_base_ids,
                )
            except MessageContentError as e:
                await sess.send_error(e.detail)
                continue

            stt = AssemblyAISTT(
                api_key=settings.assemblyai_api_key,
                sample_rate=settings.stt_sample_rate,
                flush_timeout=settings.stt_flush_timeout,
            )
            try:
                transcript, turn_errors, pending_start = await _run_stt_round(sess, stt)
            except STTError as e:
                await sess.send_error(f"语音转写失败: {e}")
                continue
            finally:
                await stt.close()

            if pending_start is not None:
                continue  # 本轮已被超越，跳过推理直接开新回合
            if turn_errors:
                # 供应商错误已即时下发 error 帧；不进 astream，回空闲
                continue

            # —— 空转写守卫 ——
            if not transcript.strip():
                await sess.send_json(SSEEvent(type="stt_empty", payload={}))
                continue

            # —— 合并终稿单帧下发（前端固化用户消息 + 锁输入的时点）——
            await sess.send_json(SSEEvent(type="stt_output", payload={"transcript": transcript}))

            # —— 终稿非空 → 镜像 POST 组装 + astream + WS 隧道 ——
            sess.busy = True  # 推理期心跳不做空闲回收
            try:
                await _run_agent_round(sess, session, agent, config, start, transcript)
            except MessageContentError as e:
                # start 校验后配置仍可能变化（附件被删等）——按回合错误处理
                await sess.send_error(e.detail)
            except _SessionClosed:
                raise
            except Exception as e:
                logger.exception("voice agent stream error: %s", e)
                await sess.send_error(f"会话推理失败: {e}")
            finally:
                sess.busy = False
            # done/error 已下发；循环回到 await_start_frame 等待下一回合
    except (WebSocketDisconnect, _SessionClosed):
        pass  # 正常断开
    except Exception as e:
        logger.exception("voice ws unexpected error: %s", e)
    finally:
        heartbeat.cancel()
        sess.closed = True
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("voice ws closed: session=%s", session_id)
