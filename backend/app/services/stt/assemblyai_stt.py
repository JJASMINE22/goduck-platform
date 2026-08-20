"""AssemblyAI Streaming STT (v3 WS) 客户端 —— 官方 voice-agent demo 形态移植。

参考：https://docs.langchain.com/oss/python/langchain/voice-agent#assemblyai-client
协议：https://www.assemblyai.com/docs/streaming/message-sequence

生命周期（一实例 = 一回合）：
- 懒连接：首帧音频才建 ``wss://streaming.assemblyai.com/v3/ws``（空闲不占供应商连接）；
- ``terminate()``：发 ``{"type":"Terminate"}`` 冲刷在途 Turn —— 官方语义：Terminate 后
  服务器先下发终稿（含格式化 Turn），最后发 ``Termination`` 并 close(1000)；
- ``receive_events()``：归一化事件流（Turn 未格式化 → stt_chunk；格式化 → stt_output），
  持续产出直至 ``Termination`` / 连接关闭 / flush 超时。格式化 Turn 不提前终止迭代：
  长按期间的自然停顿会产生中途终稿，由上层累积（多句合成一条消息），回合的结束
  权威信号是 flush（松开）触发的 terminate 收尾；
- ``stt_flush_timeout``：Terminate 后迟迟等不到收尾的兜底（超时按已得终稿收场）；
- ``close()``：随回合关闭供应商连接（幂等）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

from websockets.exceptions import ConnectionClosedError as _ConnectionClosedError
from websockets.exceptions import ConnectionClosedOK as _ConnectionClosedOK

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
    ):
        if not api_key:
            # 提前暴露配置缺失：voice_enabled=true 而 key 未配属部署错误
            raise STTError("AssemblyAI API key 未配置（assemblyai_api_key）")
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.flush_timeout = flush_timeout
        self._ws = None
        self._terminated = False

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
        if self._ws is not None:
            await self._ws.send(json.dumps({"type": "Terminate"}))

    async def receive_events(self):
        """持续消费供应商事件，直至 Termination / 连接关闭 / flush 超时。

        格式化终稿（stt_output）不结束迭代：长按期间的自然停顿会提前产生完整
        Turn，用户继续说还有后续 Turn —— 由调用方累积，回合统一在 flush 收尾。
        """
        if self._ws is None:
            # 整个按压期无一帧音频（未发音即松开/采麦失败）：空回合直接收场
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
                    if message.get("turn_is_formatted"):
                        # 终稿（format_turns=true 时同一 turn_order 两条 end_of_turn，
                        # 仅格式化那条为终 —— 只产出该条，避免双计）
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
        """关闭供应商连接（幂等；未连接时空操作）。"""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # ------------------------------------------------------------------ 内部
    async def _ensure_connection(self):
        if self._ws is not None:
            return self._ws
        url = (
            f"wss://streaming.assemblyai.com/v3/ws"
            f"?sample_rate={self.sample_rate}&format_turns=true"
        )
        headers = {"Authorization": self.api_key}
        try:
            if _WS_NEW_API:
                self._ws = await _ws_connect(url, additional_headers=headers)
            else:  # pragma: no cover - 旧版 websockets
                self._ws = await _ws_connect(url, extra_headers=headers)
        except OSError as e:
            raise STTError(f"无法连接 AssemblyAI: {e}") from e
        return self._ws

