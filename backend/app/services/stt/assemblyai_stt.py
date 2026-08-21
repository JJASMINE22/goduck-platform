"""AssemblyAI Streaming STT (v3 WS) 客户端 —— 官方 voice-agent demo 形态移植。

参考：https://docs.langchain.com/oss/python/langchain/voice-agent#assemblyai-client
协议：https://www.assemblyai.com/docs/streaming/message-sequence
模型：https://www.assemblyai.com/docs/streaming/select-the-speech-model

语音模型经 ``speech_model`` 连接参数切换（显式传入而非依赖服务端默认）：
- ``universal-3-5-pro``：旗舰（官方默认），18 语种**含中文 ZH**与日语，语种间码切换；
- ``universal-streaming-english``：仅英语（低成本）；
- ``universal-streaming-multilingual``：EN/ES/DE/FR/PT/IT（**不含中文**）。
流式 STT 不支持 ``language_code``（预录制音频专用参数）——中文能力由模型决定。

生命周期（一实例 = 一回合）：
- 懒连接：首帧音频才建 ``wss://streaming.assemblyai.com/v3/ws``（空闲不占供应商连接）；
- ``terminate()``：发 ``{"type":"Terminate"}`` 冲刷在途 Turn —— 官方语义：Terminate 后
  服务器先下发终稿（含格式化 Turn），最后发 ``Termination`` 并 close(1000)；
- ``receive_events()``：归一化事件流，终稿判定 = ``end_of_turn`` 与
  ``turn_is_formatted`` **同真**（官方 ordering guarantees 明文：仅看 formatted
  会把每个格式化 partial 当终稿——Universal-3.5 Pro 会持续下发累积全量的
  格式化 partial ``turn_is_formatted=true, end_of_turn=false``，只看 formatted
  会导致终稿文本重复累积）。partial（无论是否格式化）→ stt_chunk（覆盖语义，
  前端整段替换渲染）；格式化终稿 → stt_output，持续产出直至 ``Termination`` /
  连接关闭 / flush 超时。格式化终稿不提前终止迭代：长按期间的自然停顿会产生
  中途终稿，由上层累积（多句合成一条消息），回合的结束权威信号是 flush
  （松开）触发的 terminate 收尾；
- ``stt_flush_timeout``：Terminate 后迟迟等不到收尾的兜底（超时按已得终稿收场）；
- ``close()``：随回合关闭供应商连接（幂等）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from websockets.exceptions import ConnectionClosedError as _ConnectionClosedError
from websockets.exceptions import ConnectionClosedOK as _ConnectionClosedOK
from websockets.exceptions import WebSocketException as _WebSocketException

# websockets >= 13 提供新 asyncio 实现（additional_headers）；旧版回落 legacy
# （extra_headers）。二者协议行为一致，仅构造参数名不同。
try:  # pragma: no cover - 取决于安装版本
    from websockets.asyncio.client import connect as _ws_connect

    _WS_NEW_API = True
except ImportError:  # pragma: no cover
    from websockets import connect as _ws_connect  # type: ignore[no-redef]

    _WS_NEW_API = False

logger = logging.getLogger(__name__)


class STTError(Exception):
    """供应商侧转写错误（Error 帧 / 连接失败等），由 WS 端点转 error 帧下发。"""


@dataclass(frozen=True)
class STTEvent:
    """归一化转写事件：partial → stt_chunk（中间稿）；formatted → stt_output（终稿）。"""

    type: Literal["stt_chunk", "stt_output"]
    transcript: str


class AssemblyAISTT:
    """单回合 AssemblyAI 流式转写会话（每回合新建实例，不跨回合复用）。"""

    def __init__(
        self,
        api_key: str,
        sample_rate: int = 16000,
        flush_timeout: float = 5.0,
        speech_model: str = "universal-3-5-pro",
    ):
        if not api_key:
            # 提前暴露配置缺失：voice_enabled=true 而 key 未配属部署错误
            raise STTError("AssemblyAI API key 未配置（assemblyai_api_key）")
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.flush_timeout = flush_timeout
        self.speech_model = speech_model
        self._ws = None
        self._terminated = False
        # 连接建立或回合无音频收尾的信号：receive_events 先等它再消费——
        # 消费任务与首帧懒连接是并发的，若在连接建立前直接检查 _ws 会
        # 确定性地空收场（每次录音都被误判为空转写的根因）
        self._session_started = asyncio.Event()

    # ------------------------------------------------------------------ API
    async def send_audio(self, audio_chunk: bytes) -> None:
        """转发一段 PCM16 音频（二进制帧，原样透传，懒建立连接）。"""
        ws = await self._ensure_connection()
        await ws.send(audio_chunk)

    async def terminate(self) -> None:
        """发终止信令：服务器冲刷在途 Turn（终稿）后下发 Termination 并关连接。"""
        if self._terminated:
            return
        self._terminated = True
        if self._ws is None:
            # 整轮无音频（连接从未建立）：唤醒等待中的消费者按空收场
            self._session_started.set()
            return
        try:
            await self._ws.send(json.dumps({"type": "Terminate"}))
        except _WebSocketException:
            # 连接已死：消费者会经 ConnectionClosed 路径收尾，不重复报错
            pass

    async def receive_events(self):
        """持续消费供应商事件，直至 Termination / 连接关闭 / flush 超时。

        先等待回合启动信号（首帧音频建立连接 / 无音频回合收尾）——消费任务
        与懒连接并发启动，此处必须挂起等待而非检查即返回。
        格式化终稿（stt_output）不结束迭代：长按期间的自然停顿会提前产生完整
        Turn，用户继续说还有后续 Turn —— 由调用方累积，回合统一在 flush 收尾。
        """
        await self._session_started.wait()
        if self._ws is None:
            # 整轮无音频：正常空收场
            return
        try:
            while True:
                # Terminate 后启用兜底超时：供应商迟迟不冲刷终稿时不挂死回合
                timeout = self.flush_timeout if self._terminated else None
                raw = await asyncio.wait_for(self._ws.recv(), timeout)
                if isinstance(raw, bytes):  # pragma: no cover - v3 不下发二进制
                    continue
                message = json.loads(raw)
                mtype = message.get("type")

                if mtype == "Turn":
                    transcript = message.get("transcript") or ""
                    # 终稿契约：end_of_turn && turn_is_formatted 同真（官方明示，
                    # 否则每 turn 处理两次）。U3.5P 的格式化 partial（formatted=true
                    # 且 end_of_turn=false，transcript 为累积全量）只能按中间稿
                    # 覆盖渲染——当终稿累积会造成最终消息文本重复。
                    if message.get("end_of_turn") and message.get("turn_is_formatted"):
                        yield STTEvent(type="stt_output", transcript=transcript)
                    else:
                        yield STTEvent(type="stt_chunk", transcript=transcript)
                elif mtype == "Termination":
                    # 会话正常收尾（无在途 Turn 时的空语音路径）
                    return
                elif mtype == "Error":
                    raise STTError(
                        f"AssemblyAI 错误 {message.get('error_code')}: {message.get('error')}"
                    )
                # Begin / SpeechStarted / Heartbeat 等对会话触发无意义，跳过
        except asyncio.TimeoutError:
            logger.warning("stt flush 超时 %.1fs，按已得终稿收场", self.flush_timeout)
            return
        except _ConnectionClosedOK:
            # Terminate 后服务器收尾即 close(1000)，属正常结束
            return
        except _ConnectionClosedError as e:
            raise STTError(f"AssemblyAI 连接异常断开: {e}") from e

    async def close(self) -> None:
        """关闭供应商连接（幂等；未连接时空操作）。保证所有等待者被唤醒。"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except _WebSocketException:
                pass
            self._ws = None
        self._session_started.set()

    # ------------------------------------------------------------------ 内部
    async def _ensure_connection(self):
        if self._ws is not None:
            return self._ws
        url = (
            "wss://streaming.assemblyai.com/v3/ws"
            f"?sample_rate={self.sample_rate}"
            "&format_turns=true"
            f"&speech_model={quote(self.speech_model, safe='')}"
        )
        headers = {"Authorization": self.api_key}
        try:
            if _WS_NEW_API:
                self._ws = await _ws_connect(url, additional_headers=headers)
            else:  # pragma: no cover - 旧版 websockets
                self._ws = await _ws_connect(url, extra_headers=headers)
        except (OSError, _WebSocketException) as e:
            # 唤醒消费者（其将看到 ws=None 空收场）；错误帧由 pump 路径上报
            self._session_started.set()
            raise STTError(f"无法连接 AssemblyAI: {e}") from e
        self._session_started.set()
        return self._ws

