"""MIME 类型判定（plan §5.1 三重优先级）。

服务端唯一可信源：魔数 > 扩展名 > 客户端声明。
kind 推断：image/* → image；docx/pdf → file；其余 → file（受白名单限制）。
"""
from __future__ import annotations

import mimetypes
from typing import Optional

import filetype

# 白名单 mime（前缀匹配 + 精确匹配）
_IMAGE_PREFIX = "image/"
_ALLOWED_MIME = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
    # docx: zip 容器，魔数识别为 zip，靠扩展名二次确认
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    # pdf: 魔数 %PDF- 可直接识别，无需 docx 那套 zip 兜底
    "application/pdf",
}

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"

# 白名单内"文档类" mime → 扩展名（image/* 之外的可上传类型）。
# allowed_file_extensions() 的单一来源：新增文档类型只需同时加 _ALLOWED_MIME
# 与本表，上传门/能力查询/前端 accept 三处自动跟随。
_FILE_EXT_BY_MIME = {
    _DOCX_MIME: "docx",
    _PDF_MIME: "pdf",
}


def allowed_file_extensions() -> list[str]:
    """白名单派生的可上传文档扩展名（如 ['docx', 'pdf']）。

    供 attachment-capability 下发前端渲染 accept；与 is_allowed_mime 同源，
    消灭各处硬编码的文档类型清单。
    """
    return sorted(ext for mime, ext in _FILE_EXT_BY_MIME.items() if mime in _ALLOWED_MIME)


def file_ext_of(mime: str) -> Optional[str]:
    """文档 mime → markitdown 扩展名（StreamInfo 分派用）；非文档返回 None。"""
    return _FILE_EXT_BY_MIME.get(mime)


def detect_mime(raw: bytes, filename: Optional[str], client_mime: Optional[str]) -> str:
    """三重优先级判定真实 MIME：魔数 > 扩展名 > 客户端。"""
    # ① 魔数（最高可信度，防伪造）
    try:
        guessed = filetype.guess(raw)
        if guessed is not None:
            magic_mime = guessed.mime
            # docx 本质是 zip 容器，魔数只能给出 application/zip
            if magic_mime == "application/zip":
                # 靠扩展名二次确认是否为 docx
                ext_mime = _ext_mime(filename)
                if ext_mime == _DOCX_MIME:
                    return _DOCX_MIME
            return magic_mime
    except Exception:
        pass

    # ② 扩展名
    ext_mime = _ext_mime(filename)
    if ext_mime:
        return ext_mime

    # ③ 客户端声明（最低可信度兜底）
    if client_mime:
        return client_mime

    return "application/octet-stream"


def _ext_mime(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    ext_mime, _ = mimetypes.guess_type(filename)
    return ext_mime


def detect_kind(real_mime: str) -> str:
    """根据真实 mime 判定附件类型：image | file。"""
    if real_mime.startswith(_IMAGE_PREFIX):
        return "image"
    return "file"


def is_allowed_mime(real_mime: str) -> bool:
    """白名单校验：仅放行 image/* + docx + pdf。"""
    if real_mime.startswith(_IMAGE_PREFIX):
        return True
    return real_mime in _ALLOWED_MIME


def is_docx(mime: str) -> bool:
    return mime == _DOCX_MIME


def is_pdf(mime: str) -> bool:
    return mime == _PDF_MIME


def extract_docx_meta(raw: bytes) -> tuple[Optional[int], Optional[str]]:
    """eager 轻量提取 docx 段落数 + 首段预览（前 200 字）。

    解析失败返回 (None, None)，不影响上传。
    """
    import io

    try:
        from docx import Document

        doc = Document(io.BytesIO(raw))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        count = len(paragraphs)
        preview = paragraphs[0][:200] if paragraphs else None
        return count, preview
    except Exception:
        return None, None


def extract_pdf_meta(raw: bytes) -> tuple[Optional[int], Optional[str]]:
    """eager 轻量提取 pdf 页数 + 首页文本预览（前 200 字）。

    依赖 pymupdf (fitz)；未安装或解析失败均返回 (None, None)，不影响上传。
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        return None, None
    try:
        with fitz.open(stream=raw, filetype="pdf") as pdf:
            page_count = pdf.page_count
            first = pdf[0].get_text("text").strip() if page_count else ""
        return page_count, (first[:200] or None)
    except Exception:
        return None, None


def extract_document_meta(
    raw: bytes, mime: str
) -> tuple[Optional[int], Optional[str]]:
    """按 mime 分派文档轻量元数据提取（docx 段落数 / pdf 页数 + 预览）。

    把"按类型怎么处理"集中在本模块：上传层只需一次调用，无需关心 docx/pdf 分支。
    门禁（允不允许）由 is_allowed_mime 单独负责，与此路由职责分离。
    非文档类型或解析失败均返回 (None, None)。
    """
    if is_docx(mime):
        return extract_docx_meta(raw)
    if is_pdf(mime):
        return extract_pdf_meta(raw)
    return None, None
