"""文档解析路由：parse_document。

统一用 markitdown 把 docx/pdf 转 Markdown（保留标题层级 / 粗斜体 / 列表 / 表格结构），
替代原先 python-docx（丢格式、表格不入正文）+ pymupdf（表格拍平为竖列）的拼装方案。

判型不在此处进行：mime 已由后端上传层魔数判定并随对象持久化，这里只消费。
markitdown 实例由 core/server.py lifespan 构建并注入（进程级单例，
避免每次调用重新加载 magika ONNX 模型）。
"""
from __future__ import annotations

import asyncio
import io
import logging

from fastmcp import Context, FastMCP
from markitdown import StreamInfo

from routers._common import fetch_object

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"

# mime → markitdown 扩展名（StreamInfo 据此分派到对应 converter；
# 不走 magika 自动探测——mime 已是后端魔数判定的权威值，省去一次模型推断）
_MIME_TO_EXT = {
    _DOCX_MIME: ".docx",
    _PDF_MIME: ".pdf",
}


def register(mcp: FastMCP) -> None:
    """把文档解析工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def parse_document(
        upload_id: str,
        prompt: str = "提取全文",
        ctx: Context = None,
    ) -> dict:
        """解析用户上传的 docx/pdf 文档，返回 Markdown（保留标题/列表/表格结构）。

        Args:
            upload_id: 用户上传文档时返回的 id（必须使用附件清单中的真实 id，禁止编造）
            prompt: 解析关注点（预留，当前返回全文）
        """
        state = ctx.request_context.lifespan_context
        minio = state["minio"]
        md = state.get("markitdown")
        if md is None:
            return {
                "result": "error",
                "message": "文档解析引擎未就绪（markitdown 未安装或初始化失败）",
            }

        # ① MinIO 取字节 + 权威 mime（上传层魔数判定写入 Content-Type）
        try:
            raw_bytes, mime = fetch_object(minio, upload_id)
        except Exception as e:
            logger.warning("文档获取失败 %s: %s", upload_id, e)
            return {"result": "error", "message": f"文档获取失败: {e}"}

        if not raw_bytes:
            return {"result": "error", "message": f"无效的 upload_id: {upload_id}"}

        ext = _MIME_TO_EXT.get(mime)
        if ext is None:
            return {
                "result": "error",
                "message": f"不支持的文档类型: {mime}（仅支持 docx/pdf）",
            }

        # ② markitdown 转 Markdown（mammoth/pdfplumber 均为同步 CPU 密集，
        # 用 to_thread 包裹避免阻塞 MCP server 事件循环）
        try:
            result = await asyncio.to_thread(
                md.convert,
                io.BytesIO(raw_bytes),
                stream_info=StreamInfo(extension=ext),
            )
        except Exception as e:
            logger.warning("文档解析失败 %s: %s", upload_id, e)
            return {"result": "error", "message": f"文档解析失败: {e}"}

        markdown = result.markdown or ""
        return {
            "result": "ok",
            "upload_id": upload_id,
            "doc_type": ext.lstrip("."),
            "title": result.title,
            "preview": markdown[:200],
            "markdown": markdown,
        }
