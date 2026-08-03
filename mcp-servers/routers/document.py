"""文档解析路由：parse_document。

按上传时存入 MinIO 的 Content-Type（权威 mime）分流：
- docx → python-docx（段落 + 表格）
- pdf  → pymupdf（逐页文本）

判型不在此处进行：mime 已由后端上传层魔数判定并随对象持久化，这里只消费。
新增文档类型时，扩 _PARSERS 与对应 _parse_* 即可。
"""
from __future__ import annotations

import io
import logging

from fastmcp import Context, FastMCP

from routers._common import fetch_object

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"


def register(mcp: FastMCP) -> None:
    """把文档解析工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def parse_document(
        upload_id: str,
        prompt: str = "提取全文",
        ctx: Context = None,
    ) -> dict:
        """解析用户上传的 docx/pdf 文档，返回结构化文本。

        Args:
            upload_id: 用户上传文档时返回的 id（必须使用附件清单中的真实 id，禁止编造）
            prompt: 解析关注点（预留，当前返回全文）
        """
        state = ctx.request_context.lifespan_context
        minio = state["minio"]

        # ① MinIO 取字节 + 权威 mime（上传层魔数判定写入 Content-Type）
        try:
            raw_bytes, mime = fetch_object(minio, upload_id)
        except Exception as e:
            logger.warning("文档获取失败 %s: %s", upload_id, e)
            return {"result": "error", "message": f"文档获取失败: {e}"}

        if not raw_bytes:
            return {"result": "error", "message": f"无效的 upload_id: {upload_id}"}

        # ② 按 mime 分流解析
        if mime == _PDF_MIME:
            return _parse_pdf(upload_id, raw_bytes)
        if mime == _DOCX_MIME:
            return _parse_docx(upload_id, raw_bytes)
        return {
            "result": "error",
            "message": f"不支持的文档类型: {mime}（仅支持 docx/pdf）",
        }


def _parse_docx(upload_id: str, raw_bytes: bytes) -> dict:
    """python-docx 直接吃 BytesIO（不落临时文件），提取段落 + 表格。"""
    try:
        from docx import Document

        doc = Document(io.BytesIO(raw_bytes))
    except Exception as e:
        logger.warning("docx 解析失败 %s: %s", upload_id, e)
        return {
            "result": "error",
            "message": f"docx 解析失败（确认文件为 .docx 格式，暂不支持 .doc）: {e}",
        }

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    tables = []
    for t in doc.tables:
        tables.append([[cell.text for cell in row.cells] for row in t.rows])

    full_text = "\n".join(paragraphs)
    return {
        "result": "ok",
        "upload_id": upload_id,
        "doc_type": "docx",
        "paragraph_count": len(paragraphs),
        "table_count": len(tables),
        "preview": full_text[:200],
        "full_text": full_text,
        "tables": tables,
    }


def _parse_pdf(upload_id: str, raw_bytes: bytes) -> dict:
    """pymupdf (fitz) 逐页提取文本。依赖 pymupdf，缺失则返回可读错误。"""
    try:
        import fitz  # pymupdf
    except ImportError:
        return {
            "result": "error",
            "message": "PDF 解析依赖未安装，请执行: pip install pymupdf",
        }
    try:
        pages: list[str] = []
        with fitz.open(stream=raw_bytes, filetype="pdf") as pdf:
            page_count = pdf.page_count
            for page in pdf:
                pages.append(page.get_text("text"))
    except Exception as e:
        logger.warning("pdf 解析失败 %s: %s", upload_id, e)
        return {"result": "error", "message": f"pdf 解析失败: {e}"}

    full_text = "\n".join(pages).strip()
    return {
        "result": "ok",
        "upload_id": upload_id,
        "doc_type": "pdf",
        "page_count": page_count,
        "preview": full_text[:200],
        "full_text": full_text,
    }
