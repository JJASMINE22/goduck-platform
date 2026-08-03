"""图像解析路由：image_understand。

非多模态模型场景下，LLM 传 upload_image_id，
wrapper 内部：MinIO 取字节 → 写临时文件 → 调第三方视觉 MCP → 返回纯文本描述。
base64 / 临时文件路径 永不暴露给 LLM。
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP, Context

from routers._common import (
    fetch_bytes,
    flatten_mcp_text,
    get_tool,
    safe_remove,
    write_tmp_file,
)

logger = logging.getLogger(__name__)

# 第三方工具名（zai-mcp-server 暴露的通用图像分析工具）
THIRD_PARTY_TOOL_NAME = "analyze_image"


def register(mcp: FastMCP) -> None:
    """把图像相关工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def image_understand(
        upload_image_id: str,
        prompt: str = "解析图像内容",
        ctx: Context = None,
    ) -> dict:
        """理解用户上传的图片，返回文字描述。

        Args:
            upload_image_id: 用户上传图片时返回的 id（必须使用附件清单中的真实 id，禁止编造）
            prompt: 解析关注点，如 "图中报错信息是什么"
        """
        state = ctx.request_context.lifespan_context
        minio = state["minio"]
        tools = state["tools"]
        tmp_dir = state["tmp_dir"]

        # ① MinIO 取字节
        try:
            image_bytes = fetch_bytes(minio, upload_image_id)
        except Exception as e:
            logger.warning("图像获取失败 %s: %s", upload_image_id, e)
            return {"result": "error", "message": f"图像获取失败: {e}"}

        # ② 校验 upload_id 真实存在（防 LLM 编造）
        if not image_bytes:
            return {
                "result": "error",
                "message": f"无效的 upload_image_id: {upload_image_id}，请确认使用消息附件清单中的真实 id",
            }

        # ③ 写临时文件（第三方工具只认本地路径/URL）
        tmp_path = write_tmp_file(tmp_dir, upload_image_id, image_bytes, suffix=".png")

        try:
            tool = get_tool(tools, THIRD_PARTY_TOOL_NAME)
            raw = await tool.ainvoke({
                "image_source": tmp_path,
                "prompt": prompt,
            })
            description = flatten_mcp_text(raw)
            return {
                "result": "ok",
                "upload_image_id": upload_image_id,
                "description": description,
            }
        except Exception as e:
            logger.exception("图像解析失败 %s", upload_image_id)
            return {"result": "error", "message": f"图像解析失败: {e}"}
        finally:
            safe_remove(tmp_path)
