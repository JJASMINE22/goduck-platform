"""markitdown lazy singleton (backend process) for workspace office-doc preview.

Distinct from the mcp-servers process markitdown instance (which reads uploads
from MinIO by ``upload_id``). This one converts workspace deliverables read from
the local filesystem, serving only the workspace preview endpoint (plan §3.2-3).

Built lazily so the (heavy) magika ONNX model load happens only on the first
preview request, and only if markitdown is importable — its absence degrades the
preview endpoint to 503 without affecting any other backend functionality
(mirrors the try/except isolation in mcp-servers/core/server.py:56-66).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions markitdown converts to Markdown with structure preserved (titles /
# lists / tables / sheets). The preview endpoint is gated on this set so we
# never hand markitdown a type it cannot handle, and clients get a stable 415
# for unsupported types rather than a noisy conversion traceback.
CONVERTIBLE_EXTS = {".docx", ".xlsx", ".pptx", ".pdf", ".doc", ".xls", ".ppt", ".csv"}


@lru_cache(maxsize=1)
def get_markitdown():
    """Return a process-level MarkItDown instance, or ``None`` if unavailable.

    Cached (including the ``None`` result) so a missing or broken install is
    probed exactly once and never retried on every preview request.
    """
    try:
        from markitdown import MarkItDown
    except Exception:  # noqa: BLE001
        logger.warning("markitdown 未安装；office 文档工作区预览将返回 503")
        return None
    try:
        return MarkItDown()
    except Exception:  # noqa: BLE001
        logger.exception("markitdown 初始化失败；office 文档工作区预览将返回 503")
        return None


def convert_to_markdown(path: Path) -> str | None:
    """Convert a workspace office doc to Markdown via markitdown (sync, CPU-bound).

    Returns the Markdown text, or ``None`` if markitdown is unavailable or
    conversion failed — callers degrade to a fallback card / 503.

    markitdown infers the converter from the file extension when given a ``Path``
    source, so no magika auto-detection runs (deterministic, faster). The
    endpoint wraps this in ``asyncio.to_thread`` since the conversion is
    synchronous and CPU-bound.
    """
    md = get_markitdown()
    if md is None:
        return None
    try:
        result = md.convert(path)
        return getattr(result, "markdown", None) or ""
    except Exception:  # noqa: BLE001
        logger.warning("markitdown 转换失败: %s", path, exc_info=True)
        return None
