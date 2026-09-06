"""langchain-openai 1.x 故意不提取 DeepSeek/GLM 的 `reasoning_content`。

其源码多处注明："reasoning_content, reasoning_details are **not** extracted. Use a
provider-specific subclass."（只认 OpenAI 原生 `reasoning`）。GLM 的思考内容走
`reasoning_content`（DeepSeek 约定），会被直接丢弃，导致思考内容提取
（本模块 ``extract_reasoning``）无内容可取。

本模块在响应转换入口（_convert_dict_to_message 非流式 / _convert_delta_to_message_chunk
流式）注入提取：把原始响应里的 reasoning_content 写入 AIMessage(Chunk).additional_kwargs。
流式聚合时 additional_kwargs 的字符串值会被 langchain 拼接，故逐 chunk 注入可正确累加。

幂等：进程内只 patch 一次。
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

_applied = False


def extract_reasoning(msg) -> Optional[str]:
    """Best-effort 思考内容提取（reasoning / thinking）for a COMPLETE message。

    实时流的原子兜底（stream_adapter，未经 token 流式的 AI 消息）与历史回放
    （session_service._serialize_message，checkpoint 重放消息）共用本函数，
    保证"思考过程"面板在两条路径下逐字一致。覆盖两类载体：
    - GLM/DeepSeek/Qwen：``additional_kwargs['reasoning_content']``（本模块
      patch 注入，随 checkpoint 持久化）；
    - Anthropic 风格：content blocks 中 type 为 'thinking'/'reasoning' 的文本。
    无思考内容时返回 None（纯空白视为无）。
    """
    ak = getattr(msg, "additional_kwargs", None) or {}
    rc = ak.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                text = block.get("thinking") or block.get("text") or block.get("reasoning")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if parts:
            return "\n\n".join(parts)
    return None


def apply_once() -> None:
    """Patch langchain_openai 的两个消息转换函数以透传 reasoning_content。幂等。"""
    global _applied
    if _applied:
        return
    try:
        from langchain_core.messages import AIMessage, AIMessageChunk
        import langchain_openai.chat_models.base as base

        _orig_to_message = base._convert_dict_to_message
        _orig_to_chunk = base._convert_delta_to_message_chunk

        def _inject_reasoning(message: Any, _dict: Mapping[str, Any]) -> Any:
            rc = _dict.get("reasoning_content") if isinstance(_dict, Mapping) else None
            if isinstance(rc, str) and rc and isinstance(message, (AIMessage, AIMessageChunk)):
                message.additional_kwargs["reasoning_content"] = rc
            return message

        def _patched_to_message(_dict: Mapping[str, Any]):
            return _inject_reasoning(_orig_to_message(_dict), _dict)

        def _patched_to_chunk(_dict: Mapping[str, Any], default_class: type):
            return _inject_reasoning(_orig_to_chunk(_dict, default_class), _dict)

        base._convert_dict_to_message = _patched_to_message
        base._convert_delta_to_message_chunk = _patched_to_chunk
        _applied = True
        logger.info("reasoning_content extraction patch applied (langchain_openai)")
    except Exception as e:  # noqa: BLE001
        logger.warning("应用 reasoning_content 透传 patch 失败: %s", e)
