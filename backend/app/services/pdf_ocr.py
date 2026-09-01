"""打印件 PDF（无文本层扫描件）OCR 兜底：RapidOCR（PP-OCR ONNX）逐页识别。

背景：扫描件 PDF 的页面只有整页位图，pymupdf ``get_text`` 逐页返回空串 ——
上层 ``RecursiveCharacterTextSplitter`` 拿不到任何文本（入库报"无可索引文本"）。
本模块在"文本层提取"与"切块"之间补一步 OCR：

- 页级判定（混合文档天然兼容）：单页文本层字符数 < 阈值 → 该页渲染灰度位图
  → OCR；有原生文本层的页零成本直通（原生文本恒优于 OCR，不重复识别）。
- 引擎懒加载单例 + 进程内推理串行锁：ONNX session 并发调用非线程安全，
  且 CPU 推理并行只会互抢核（kb_ingestion_concurrency>1 时自然排队）。
- 进度回调 ``on_page(done, total)``：在工作线程内被调（本模块恒被
  ``asyncio.to_thread`` 驱动），异步侧负责向事件循环封送。
- 未安装 rapidocr-onnxruntime：``ocr_available()`` 为 False，调用方降级为
  仅文本层提取（行为同旧版），不 import 不报错。

识别后处理：按检测框 top-y/top-x 排序恢复阅读顺序（RapidOCR 返回顺序仅
近似），低置信度框丢弃（扫描噪声），逐框换行拼接交给上层 splitter。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Optional

logger = logging.getLogger(__name__)

# 单框（行/片段）置信度下限：PP-OCR 正常文本通常 >0.85，低于此多为噪声
_MIN_BOX_SCORE = 0.45

_engine: Optional[object] = None
_engine_lock = threading.Lock()
# 推理串行锁：跨线程共享一个 ONNX session 不安全；CPU 下串行也更快
_infer_lock = threading.Lock()


def ocr_available() -> bool:
    """rapidocr-onnxruntime 是否已安装（不触发引擎加载，供降级判定/提示）。"""
    try:
        import rapidocr_onnxruntime
    except ImportError:
        return False
    return True


def _get_engine() -> object:
    """懒加载引擎单例（首次调用加载 det/rec/cls 三个 ONNX 模型，~秒级）。"""
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
            logger.info("RapidOCR 引擎已加载（CPU / PP-OCR ONNX）")
    return _engine


def page_needs_ocr(text: str, min_chars: int) -> bool:
    """页级判定：文本层字符数不足阈值 → 视为扫描页，需要 OCR。

    阈值而非布尔：原生页也可能只有几个字符（章节扉页/图表页），严格
    "零字符才 OCR" 会让这类页漏识别；阈值化后误伤成本仅为一次冗余 OCR。
    """
    return len(text.strip()) < min_chars


def assemble_ocr_text(result: list) -> str:
    """RapidOCR result（[[box, text, score], ...]）→ 阅读顺序文本。

    box 为四点坐标（左上角点 ``box[0]``），按 (top-y, top-x) 排序恢复
    "从上到下、从左到右"；近行合并属过度工程——splitter 对换行不敏感。
    """
    items: list[tuple[float, float, str]] = []
    for box, text, score in result:
        if score < _MIN_BOX_SCORE or not text.strip():
            continue
        items.append((float(box[0][1]), float(box[0][0]), text.strip()))
    items.sort(key=lambda t: (t[0], t[1]))
    return "\n".join(t[2] for t in items)


def _ocr_image(png: bytes) -> str:
    """单页位图 → 文本（引擎推理，进程内串行）。异常上抛由调用方按页兜底。"""
    engine = _get_engine()
    with _infer_lock:
        result, _ = engine(png)
    if not result:
        return ""
    return assemble_ocr_text(result)


def extract_pdf_pages(
    raw: bytes,
    *,
    dpi: int = 200,
    min_chars: int = 16,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> tuple[list[tuple[int, str]], int]:
    """逐页提取（原生文本层优先，扫描页 OCR 兜底）。

    → (pages, ocr_pages)：pages = [(page_no, text)]（与旧 _extract_pages 同形，
    空页保留——空页过滤由上层切块阶段统一处理）；ocr_pages = 尝试过 OCR 的
    页数（含失败页；0=纯原生文档，调用方据此跳过缓存与进度分段）。

    单页 OCR 失败不致命：记警告、保留原生（通常为空）文本，继续后续页——
    955 页扫描件不应因 1 页渲染/识别异常整体报废。
    恒在 worker 线程调用（CPU 密集，禁止事件循环直跑）。
    """
    import pymupdf

    pages: list[tuple[int, str]] = []
    ocr_pages = 0
    with pymupdf.open(stream=raw, filetype="pdf") as pdf:
        total = pdf.page_count
        for i in range(total):
            text = pdf[i].get_text("text")
            if page_needs_ocr(text, min_chars):
                ocr_pages += 1  # 口径=尝试数（失败页也计数：进度/缓存判定一致）
                try:
                    pix = pdf[i].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
                    ocr_text = _ocr_image(pix.tobytes("png"))
                    if len(ocr_text.strip()) > len(text.strip()):
                        text = ocr_text
                except Exception:
                    logger.warning(
                        "pdf 第 %d 页 OCR 失败，保留原生文本", i + 1, exc_info=True
                    )
            pages.append((i + 1, text))
            if on_page is not None:
                on_page(i + 1, total)
    return pages, ocr_pages
