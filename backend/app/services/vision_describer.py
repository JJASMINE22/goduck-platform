"""VisionDescriber：图像 → 文本描述（one-shot 视觉预消费，plan §3.4）。

窄接口契约：入参 data URL + 预 prompt，出参纯文本；任何失败统一
``DescriberError``（上层降级为无图 content，不中断回合）。厂商数据格式
差异终结在本边界内，永不泄漏到 ContentComposer / 策略层。

- 实现唯一内置：openai_compatible（经 ModelStore 缓存客户端 ainvoke）。
  思考模式由模型行配置约定：注册视觉下挂模型时设 enable_thinking=false
  （描述为 one-shot 短任务，思考徒增延迟与成本——glm-4.6v 实测开思考
  558 tokens vs 关闭 482，且首 token 显著更慢）；编排页候选列表经
  GET /models?supports_vision=true&enable_thinking=false 过滤，注册表单
  有对应提示。zai-sdk / 其它原生 SDK 留作未来 provider_kind 分派位
  （引入信号：无 OpenAI 兼容端点 / 厂商特有能力 / 转换有损）。
- 固定预 prompt 模板与描述缓存一致性耦合：模板固定 ⇒ 同一 upload_id
  描述跨轮复用；改模板须失效缓存（清 upload.vision_desc）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Optional

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from app.services.model_store import ModelStore

logger = logging.getLogger(__name__)

# 固定预 prompt（缓存一致性的一部分：与 upload.vision_desc 绑定，改动=失效全部缓存）
DESCRIBE_PROMPT = (
    "请详细、客观地描述这张图片的全部可见内容：场景与物体及其属性"
    "（颜色/位置/数量）、人物与动作、图表数据、界面元素；图片中出现的"
    "任何文字（标签、报错信息、标注）必须逐字转写。用简洁的中文陈述句输出，"
    "不要添加猜测性内容。"
)


class DescriberError(Exception):
    """视觉描述失败（超时/模型不可用/响应异常）——上层统一降级，不中断回合。"""


def _flatten_text(content) -> str:
    """AIMessage.content（str 或 content blocks）→ 纯文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return str(content).strip()


class VisionDescriber:
    """经已注册视觉模型（ModelStore describe_client）生成图像文本描述。"""

    def __init__(self, model_store: "ModelStore", timeout_s: float):
        self._model_store = model_store
        self._timeout_s = timeout_s

    async def describe(
        self,
        vision_model_id: uuid.UUID,
        image_data_url: str,
        prompt: Optional[str] = None,
    ) -> str:
        """单图描述。失败抛 DescriberError；返回非空纯文本（空响应也视为失败）。"""
        try:
            client = self._model_store.get(vision_model_id)
        except KeyError as e:
            raise DescriberError(f"视觉模型不可用: {e}") from e

        message = HumanMessage(
            content=[
                {"type": "text", "text": prompt or DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
        )
        try:
            # ainvoke 契约：str | PromptValue | Sequence[BaseMessage]——
            # 单条消息也必须包成列表，裸 HumanMessage 会被
            # _convert_to_messages 拒绝（Invalid input type）
            resp = await asyncio.wait_for(client.ainvoke([message]), timeout=self._timeout_s)
        except asyncio.TimeoutError as e:
            raise DescriberError(f"视觉描述超时（>{self._timeout_s:.0f}s）") from e
        except Exception as e:  # noqa: BLE001 — 网络/429/格式异常统一降级语义
            raise DescriberError(f"视觉描述调用失败: {e}") from e

        text = _flatten_text(getattr(resp, "content", None))
        if not text:
            raise DescriberError("视觉描述返回空内容")
        return text
