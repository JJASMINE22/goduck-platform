"""MIME 类型判定（plan §5.1 三重优先级）。

服务端唯一可信源：魔数 > 扩展名 > 客户端声明。
kind 推断：image/* → image；docx → file；其余 → file（受白名单限制）。
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
}

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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
    """白名单校验：仅放行 image/* + docx。"""
    if real_mime.startswith(_IMAGE_PREFIX):
        return True
    return real_mime in _ALLOWED_MIME


def is_docx(mime: str) -> bool:
    return mime == _DOCX_MIME


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
