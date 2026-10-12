"""ToolMessage/AIMessage content → 字符串提取（SSE 与历史序列化共用）。

背景：langchain-mcp-adapters（0.3.x）把 MCP 工具返回包成 LangChain 内容块
列表（``TextContent → {"type": "text", "text": ...}``），ToolMessage.content
因此常为 ``[{"type": "text", "text": "<JSON>"}]``。此前序列化层
（stream_adapter._content_str / session_service._serialize_message）对非
字符串 content 一律 ``str()``，产出 Python repr（单引号容器），前端
``JSON.parse`` 必败——kb_search 卡片据此把成功检索误显示为「检索失败」，
并连带命中渲染与证据锚定预览整链不渲染。

提取语义（与 core/jev_kb_gate._extract_text 同源，但支持多块拼接）：

- str 原样返回（多数本地工具与归一化错误消息的形态）；
- list：按序收集 ``type == "text"`` 且 ``text`` 为 str 的块，``"\\n"`` 拼接；
  无任何文本块（图像/文件块等非文本载荷）→ 回落 ``str(content)``，
  保持旧观测形态（有内容可见）而非静默丢弃；
- 其余类型回落 ``str(content)``（防御：未知容器形态不抛出）。
"""
from __future__ import annotations

from typing import Any


def content_to_str(content: Any) -> str:
    """消息 content → 展示/传输用字符串。永不抛出，恒返回 str。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    try:
        return str(content)
    except Exception:  # noqa: BLE001 — 展示层提取永不杀死 SSE/序列化
        return f"<{type(content).__name__} object>"
