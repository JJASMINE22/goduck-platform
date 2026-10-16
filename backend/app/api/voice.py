"""语音输入 WS 端点（plan：单 WS 全管线）。

``WS /sessions/{session_id}/voice``：一个连接承载完整语音管线
``浏览器音频 ⇄ 后端 ⇄ AssemblyAI(STT)``。连接**常驻**（会话页挂载即建、
跨回合复用、应用层心跳保活），语音回合按需懒建供应商连接。

回合协议（外层回合循环，done/stt_empty 后不关连接，回到空闲等 start）：
- 上行 start（按下）：草稿/附件/KB/rubric 快照 → 镜像 POST 校验（错误在录音前暴露）；
- 上行 audio（按住期间）：PCM16 帧原样转发供应商（懒连接）；
- 上行 flush（松开）：Terminate 供应商 → 冲刷终稿；
- 上行 cancel（推理期）：取消当前 run（前端"中止/先停后撤"信号；RunManager
  广播 canceled 终态，前端据其落态——与 SSE 路径同契约），回合循环保持；
- 下行 stt_chunk/stt_output/stt_empty：转写事件直通；
- 终稿非空 → 草稿+终稿合并为一条消息 → 镜像 POST 组装（附件/KB 锚点）→
  RunManager run（同 thread_id 记忆续接）→ agent 事件经 WS 下行隧道（与 SSE
  事件同名同构，前端复用 dispatch）。

守卫：
- 空转写（未发音/纯呼吸声/flush 超时）→ stt_empty 帧，不进 astream，草稿
  保留在前端（合并不发生，用户手打内容不被吞）；
- 推理经 RunManager 以 kind=voice 启动 run（单写者闸与 POST/withdraw 互斥），
  cancel/断连/自然收口统一取消或等待 run 终态（withdraw 的 409 闸随取消即时
  放行）；
- 空闲 10min 服务端 close(4408) 回收；供应商错误 → error 帧、回合作废、连接保持。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from websockets.exceptions import WebSocketException

from app.api.stream import (
    _build_agent_or_409,
    _persist_usage,
    _refuse_or_heal_pending,
    clear_orphan_pending,
    join_cancelled_run,
    resolve_agent_choice_or_409,
)
from app.deps import SimpleUser, get_ws_appstate
from app.models import db as dbmod
from app.models.schemas import VoiceStartFrame
from app.services.message_content import (
    MessageContentError,
    validate_message_content,
)
from app.services.run_manager import (
    NoSuchRun,
    ResyncNeeded,
    RunConflictError,
    RunLimitError,
)
from app.services.run_source import chat_event_source
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


# ---------------------------------------------------------------------------
# 句首语气词清洗
# ---------------------------------------------------------------------------
# 长按从按压瞬间即推流，发音前的呼吸/开口音会被 Universal-3.5-Pro 转写为
# 填充词（"Hmm,"/"嗯，" 等；流式协议无 disfluency 关闭开关，模型侧 VAD 只负责
# 切 turn、不去除句首填充）。终稿进入 astream 前剥离句首填充词：
# - 英文：hmm/mmm/um/uh/er/ah/eh/oh（须后随空白/标点或独占整段，避免误伤
#   "umbrella"/"Ohio" 等正常词首）；
# - 中文：嗯/呃/唉/诶（保守集合，"啊/哦" 可承载语义故保留）；
# 循环剥离以清掉连续多个填充；剥离后为空的分句整体丢弃。
_LEADING_FILLER = re.compile(
    r"^(?:(?:h{0,2}m{2,}|e?m{2,}|um+|uh+|er+|ah+|eh+|oh+)(?:[,.!?\s]+|$)"
    r"|[嗯呃唉诶]{1,3}[，。、\s]*)",
    re.IGNORECASE,
)


def _strip_leading_fillers(text: str) -> str:
    prev: str | None = None
    while text != prev:
        prev = text
        text = _LEADING_FILLER.sub("", text, count=1)
    return text.strip()


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
    if session.get("kind") == "cron":
        # 任务会话只读：语音输入属"主动发言"，与 POST /messages 同拒（对齐 stream.py 守卫）
        raise PermissionError("cron session is read-only")
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
                except (STTError, OSError, WebSocketException) as e:
                    # 供应商连接失败/中断：回合仍由 flush 收口，后续音频丢弃
                    turn_error = True
                    errors.append(f"语音转写服务异常: {e}")
                    await sess.send_error(errors[-1])

    async def consume() -> None:
        """供应商事件 → 下行帧。全部按 stt_chunk 渲染（partial 为覆盖语义，前端
        整段替换渲染；完整终稿在 flush 后由端点合并为单条 stt_output 下发——前端
        据此一次性固化用户消息，多句长按天然合成一条消息）。"""
        gen = stt.receive_events()
        try:
            async for ev in gen:
                await sess.send_json(
                    SSEEvent(type="stt_chunk", payload={"transcript": ev.transcript})
                )
                if ev.type == "stt_output":
                    finals.append(ev.transcript)
        except STTError as e:
            # 供应商异常：error 帧已由 pump 路径上报（下次 send_audio 必同步失败），
            # 此处静默收尾并保留已收集的 finals，避免重复报错
            logger.warning("stt consumer aborted: %s", e)
        finally:
            # 确定性关闭生成器：取消/异常路径若仅弃置（悬挂在 yield 处），GC 终结器
            # 的 athrow 任务会留 "Task exception was never retrieved" 噪声
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await gen.aclose()

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
    # 句首语气词剥离（"Hmm,"/"嗯，" 等按压瞬间呼吸声被转写的填充词）；
    # 剥离后为空的分句（纯填充 turn）整体丢弃
    cleaned = (p for p in (_strip_leading_fillers(part) for part in finals if part) if p)
    return " ".join(cleaned), errors, None


async def _run_agent_round(
    sess: _VoiceSession,
    session: dict,
    agent,
    config: dict,
    start: VoiceStartFrame,
    user_text: str,
    agent_id: uuid.UUID | None = None,
) -> dict | None:
    """镜像 POST /messages：组装 content → run（RunManager）→ WS 下行隧道。

    user_text 为"草稿+终稿"合并稿（voice_ws 主循环合并后传入），同时作为
    user_query（content 组装）与 raw_content（历史/回撤回填的原文），与前端
    固化的用户消息严格一致。

    推理期并发监视上行帧（此前推理期无人读接收侧，中止信令无处落地——这正是
    语音 query 无法中止/回撤的操作冲突根因）：
    - ``cancel``：前端中止/先停后撤信号 → RunManager 取消 run；canceled 终态
      经隧道下发（与 SSE 路径同契约，前端据其落态），会话闸随终态即时释放；
    - ``start``：防御性超越（正常前端推理期锁输入不会发）→ 取消本轮，start
      原样交还回合循环开新回合——按键永不丢失；
    - 其余帧（pong/二进制）消费后丢弃；断连（receive 抛 WebSocketDisconnect）
      同样即时取消 run——静默长推理不再占用会话闸，withdraw 不被卡（语音
      场景跟随 cancel 语义，仅 SSE 场景后台续跑）。

    返回超越 start 帧或 None。
    """
    st = sess.st
    thread_id = session["thread_id"]
    superseded_start: dict | None = None

    factory = chat_event_source(
        st,
        session,
        agent,
        config,
        agent_id=agent_id,
        user_query=user_text,
        attachments=start.attachments,
        knowledge_base_ids=start.knowledge_base_ids,
        rubric=start.rubric,
        raw_content=user_text,
    )

    async def on_usage(payload: dict) -> None:
        # usage 与 SSE 路径同构即时入库（best-effort，见 _persist_usage）
        await _persist_usage(st.pool, session["id"], payload)

    async def on_final(_final_event) -> None:
        await SessionService(st.pool).touch(session["id"])

    try:
        await st.run_manager.start_run(
            thread_id, session["id"], "voice", factory,
            on_usage=on_usage, on_final=on_final,
        )
    except RunConflictError as e:
        await sess.send_error(f"该会话已有进行中的回合：{e}")
        return None
    except RunLimitError as e:
        await sess.send_error(str(e))
        return None

    async def tunnel() -> None:
        # 订阅 run → WS 下行：done/interrupt/error/canceled 终态由 run 产出，
        # 前端据其落态；发送失败抛 _SessionClosed 由上层统一收尾
        try:
            async for _event_id, ev in st.run_manager.subscribe(thread_id):
                await sess.send_json(ev)
        except (NoSuchRun, ResyncNeeded):
            logger.warning("voice tunnel lost run for thread %s (defensive)", thread_id)

    async def watch() -> None:
        nonlocal superseded_start
        while True:
            msg = await sess.receive()  # 断连抛 WebSocketDisconnect
            text = msg.get("text")
            if text is None:
                continue  # 推理期无录音，二进制帧丢弃
            try:
                ctrl = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(ctrl, dict):
                continue
            ctype = ctrl.get("type")
            if ctype in ("cancel", "start"):
                if ctype == "start":
                    superseded_start = ctrl
                return
            # pong / 其它控制帧：活性已由 receive 记录，忽略

    tunnel_task = asyncio.create_task(tunnel())
    watcher = asyncio.create_task(watch())
    done, _pending = await asyncio.wait(
        {tunnel_task, watcher}, return_when=asyncio.FIRST_COMPLETED
    )
    if watcher in done:
        watcher_exc = watcher.exception() if not watcher.cancelled() else None
        # cancel / 超越 start / 断连：终止 run。canceled 终态经隧道下发后自然
        # 收口（连接已死时无人消费，仅取消即可）；取消后的残余异常不再下发，
        # 仅断连向上传播走统一收尾
        run = st.run_manager.cancel(thread_id)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await tunnel_task
        # 图状态收尾（与 SSE cancel 同手法）：join run task 终止后清孤儿
        # pending。best-effort——断连场景失败无妨，残留由下一回合的门禁
        # 自愈（_refuse_or_heal_pending）兜底。须在 raise 之前执行。
        if run is not None and await join_cancelled_run(run):
            with contextlib.suppress(Exception):
                await clear_orphan_pending(st, session)
        if watcher_exc is not None:
            raise watcher_exc
    else:
        # 自然收尾（终态已下发）：停监视器，透传 tunnel 异常
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await tunnel_task
    return superseded_start


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

    # 连接级校验：会话存在/未归档（失败 → error 帧 + close）。
    # 智能体身份的权威解析在回合级（start 帧 agent_id > last_agent_id，见回合
    # 循环），连接级取图只是预热 best-effort：空会话（尚未选择）与悬空 last
    # （agent 已删）都是合法进入态——不关闭连接（麦克风 UI 在未选择时本就
    # 置灰；若 start 帧解析失败，回合级会给出带上下文的 error 帧）。仅
    # "last 存在但图不可构建"（模型被禁用等配置性硬错误）沿用 fail-fast。
    try:
        session, _svc = await _load_session(st, session_id)
    except KeyError:
        await _session_gone(sess, _CLOSE_SESSION_GONE, "session not found")
        return
    except PermissionError as e:
        await _session_gone(sess, _CLOSE_SESSION_GONE, str(e))
        return
    agent = None
    last_agent = session.get("last_agent_id")
    if last_agent is not None and await dbmod.get_agent(st.pool, last_agent) is not None:
        try:
            agent = await _build_agent_or_409(st, last_agent)
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

            # 回合级智能体解析（写者语义，§4.4）：start 帧 agent_id 优先，
            # 缺省回落 last_agent_id。逐回合刷新 session 快照 + 重解析（连接
            # 常驻，期间 last 可能被并行 SSE 回合更新或 agent 被删除）；
            # 失败 → error 帧、回合作废、连接保持。
            try:
                fresh = await SessionService(st.pool).get(session_id)
                if fresh is not None:
                    session = fresh
                chosen_id = await resolve_agent_choice_or_409(st, session, start.agent_id)
                agent = await _build_agent_or_409(st, chosen_id)
            except HTTPException as e:
                await sess.send_error(str(e.detail))
                continue

            # HITL 门禁 + 中止残留自愈（§4.4，与 POST /messages 同语义）：真
            # 中断拒绝新回合；cancelled/崩溃残留的孤儿 pending 清尾后放行。
            try:
                await _refuse_or_heal_pending(agent, config)
            except HTTPException as e:
                await sess.send_error(str(e.detail))
                continue
            except Exception:
                # 状态读取/自愈失败不阻断回合（沿用原语义；exc_info 告警自证）
                logger.warning("voice gate state check failed", exc_info=True)

            # start 快照校验（录音前暴露配置错误；失败 → error 帧、回合作废）
            try:
                await validate_message_content(
                    st, session,
                    agent_id=chosen_id,
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
                speech_model=settings.stt_speech_model,
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

            # —— 空转写守卫（位于草稿合并之前：未发音不吞草稿，前端保留 draft）——
            if not transcript.strip():
                await sess.send_json(SSEEvent(type="stt_empty", payload={}))
                continue

            # —— 草稿 + 终稿合并为一条消息（草稿在前、换行分隔；空草稿即纯终稿）——
            draft = (start.draft or "").strip()
            user_text = f"{draft}\n{transcript}" if draft else transcript

            # —— 合并稿单帧下发（前端固化用户消息 + 清草稿 + 锁输入的时点；
            #     帧内文本与发往 agent 的 user_query 严格一致，所见即所发）——
            await sess.send_json(SSEEvent(type="stt_output", payload={"transcript": user_text}))

            # —— 合并稿 → 镜像 POST 组装 + astream + WS 隧道 ——
            sess.busy = True  # 推理期心跳不做空闲回收
            superseded: dict | None = None
            # last_agent_id 唯一写点（§4.4 写者语义，与 POST /messages 对齐）：
            # 转写落定、即将开 run 时写入（空转写不写——没有消息就没有"最近使用"）
            await dbmod.set_session_last_agent(st.pool, session["id"], chosen_id)
            try:
                superseded = await _run_agent_round(
                    sess, session, agent, config, start, user_text, agent_id=chosen_id
                )
            except MessageContentError as e:
                # start 校验后配置仍可能变化（附件被删等）——按回合错误处理
                await sess.send_error(e.detail)
            except (WebSocketDisconnect, _SessionClosed):
                raise
            except Exception as e:
                logger.exception("voice agent stream error: %s", e)
                await sess.send_error(f"会话推理失败: {e}")
            finally:
                sess.busy = False
            if superseded is not None:
                pending_start = superseded  # 推理被新 start 超越：直接开新回合
                continue
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
