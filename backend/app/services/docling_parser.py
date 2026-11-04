"""知识单元解析适配层（plan: kb-docling-parse-display，B1）。

职责：把 pdf/docx 原始字节解析为知识单元 IR v1.1（契约见
``backend/schema/knowledge-unit.schema.json``），供入库管线直入 ES。

- pdf：docling 标准管线（layout + TableFormer ACCURATE + RapidOCR ``lang=["ch"]``
  仅扫描页兜底——docling 页级判定：原生文本层页不付 OCR 成本）；长文档分片
  （shard=(start,end)，1-based 闭区间），**分片只换 page_range 不重建模型**。
- docx：docx backend 声明式直解（无 ML、无 OCR、无 shard）——OOXML 结构单元
  （w:p/w:tbl/样式标记）比 PDF 推断更权威；无页面几何 ⇒ page/bbox/doc_uri
  恒 None（B2 可空契约，前端文本态降级）。
- 预算护栏：单元超 ``unit_max_chars`` 时在子区域边界再切（行/句/表行），
  切分携带 heading_path，禁止字符盲切；宽表按行拆分重复表头。
- 常驻单例纪律（重资产转换器通用纪律）：``DocumentConverter`` 懒加载 + 初始化
  锁 + convert 串行锁（CPU 下并行 convert 只会互抢核）；内存驻留 ~1.5-2GB，
  首次解析冷启动（模型加载数十秒）由调用方在进度模型留段。

docling 为软依赖（同 rapidocr 纪律）：未安装时 ``docling_available()`` 为
False，调用方（knowledge_service docling 路径）在入库前显式拦截为 failed，
**绝不静默回落 legacy**——操作者显式选择 docling 引擎，静默回落会造成
数据形态与预期不符。

书签校验源（heading_path 对齐验证）由调用方经 pymupdf ``get_toc()`` 独立
读取，不在本模块职责内。
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.services.mime_utils import is_docx, is_pdf

logger = logging.getLogger(__name__)

#: IR 契约版本（schema/knowledge-unit.schema.json 同源；形状变更必须升版本，
#: 既有纪律：边车缓存形状不匹配视为未命中）。
IR_VERSION = "1.1"

# 入栈标题（阅读序标题栈）：docling label 字符串值（enum 的 .value / str 双兼容）
_HEADING_LABELS = frozenset({"title", "section_header"})
# 跳过的噪声 label：页眉/页脚逐页重复，入库只制造噪音块
_SKIP_LABELS = frozenset({"page_header", "page_footer"})
# 无层级信息（旧版 iterate_items 不回传 level）时标题栈深度上限，防长手册无限增长
_MAX_HEADING_DEPTH = 4

# 预算护栏：句边界切分（中英标点皆收，分隔符保留在前段尾部）
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;.!?])\s*")


# ============================ IR v1.1 ======================================


@dataclass
class KnowledgeUnit:
    """知识单元（schema/knowledge-unit.schema.json 的运行时形态）。

    ``lang``/``doc_uri`` 由入库层填充（解析器恒置 None）——语言检测与
    MinIO 归档键依赖文档级上下文（doc_id/doc_version），不属解析职责。
    """

    kind: str                       # "text" | "table" | "picture"
    text_ctx: str                   # 原文（不拼接 heading_path）
    heading_path: list[str] = field(default_factory=list)
    unit_index: int = 0             # 分片内序号（parse 按片调用、片内 0..N 回填；入库层跨片全局重编后作 chunk_index）
    page: Optional[int] = None      # 全局页码；docx=None
    page_rotate: Optional[int] = None
    bbox: Optional[dict] = None     # 归一化 {x0,y0,x1,y1}，左上原点 0..1；docx=None
    table_html: Optional[str] = None
    lang: Optional[str] = None
    doc_uri: Optional[str] = None
    assets: list[dict] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "text_ctx": self.text_ctx,
            "heading_path": list(self.heading_path),
            "unit_index": self.unit_index,
            "page": self.page,
            "page_rotate": self.page_rotate,
            "bbox": self.bbox,
            "table_html": self.table_html,
            "lang": self.lang,
            "doc_uri": self.doc_uri,
            "assets": [dict(a) for a in self.assets],
        }


def units_to_payload(units: list[KnowledgeUnit]) -> list[dict]:
    return [u.to_payload() for u in units]


def units_from_payload(data: object) -> Optional[list[KnowledgeUnit]]:
    """边车缓存反序列化：形状不匹配（版本漂移/字段缺失/类型错位）一律 None
    （视为未命中，重跑解析），绝不带病复活缓存。"""
    if not isinstance(data, list):
        return None
    out: list[KnowledgeUnit] = []
    for entry in data:
        if not isinstance(entry, dict):
            return None
        try:
            unit = KnowledgeUnit(
                kind=str(entry["kind"]),
                text_ctx=str(entry["text_ctx"]),
                heading_path=[str(h) for h in entry.get("heading_path") or []],
                unit_index=int(entry.get("unit_index") or 0),
                page=_opt_int(entry.get("page")),
                page_rotate=_opt_int(entry.get("page_rotate")),
                bbox=_validate_bbox(entry.get("bbox")),
                table_html=_opt_str(entry.get("table_html")),
                lang=_opt_str(entry.get("lang")),
                doc_uri=_opt_str(entry.get("doc_uri")),
                assets=_validate_assets(entry.get("assets")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if unit.kind not in ("text", "table", "picture"):
            return None
        out.append(unit)
    return out


def _opt_int(v: object) -> Optional[int]:
    return v if isinstance(v, int) else None


def _opt_str(v: object) -> Optional[str]:
    return v if isinstance(v, str) else None


def _validate_bbox(raw: object) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "x0": float(raw["x0"]), "y0": float(raw["y0"]),
            "x1": float(raw["x1"]), "y1": float(raw["y1"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _validate_assets(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for a in raw:
        if isinstance(a, dict):
            out.append(
                {
                    "kind": str(a.get("kind") or "picture"),
                    "page": _opt_int(a.get("page")),
                    "bbox": _validate_bbox(a.get("bbox")),
                    "caption": _opt_str(a.get("caption")),
                    "thumb_b64": _opt_str(a.get("thumb_b64")),
                }
            )
    return out


# ============================ 软依赖与单例纪律 ==============================


def docling_available() -> bool:
    """docling 是否已安装（不触发模型加载，供入库前拦截判定）。"""
    try:
        import docling.document_converter  # noqa: F401
    except ImportError:
        return False
    return True


# 懒加载单例：PDF 管线转换器（重，~1.5-2GB 常驻）；docx 转换器（声明式，轻）
_pdf_converter: Optional[object] = None
_pdf_converter_params: Optional[tuple] = None
_docx_converter: Optional[object] = None
_converter_lock = threading.Lock()
# convert 串行锁：跨线程共享转换器非线程安全；CPU 下串行也更快（并行度旋钮 =
# kb_docling_threads / 设备旋钮 = kb_docling_device，不做多实例并行 convert）
_convert_lock = threading.Lock()

#: 推理设备合法值（与 config.kb_docling_device 同源；parse 是公共入口，独立防御）
_ALLOWED_DEVICES = frozenset({"auto", "cpu", "cuda", "mps"})


def _normalize_device(device: object) -> str:
    """设备值规范化：None/空白 → auto；容忍大小写与空白；非法值入口显式拒绝。

    提前于 ``AcceleratorOptions`` 的 pydantic 校验：裸 ValidationError 栈深、
    报错定位差，入口给出可行动信息（软依赖版本漂移下仍是可读失败）。
    """
    val = str(device or "").strip().lower() or "auto"
    if val not in _ALLOWED_DEVICES:
        raise ValueError(
            f"docling 推理设备非法：{device!r}（合法值 auto/cpu/cuda/mps）"
        )
    return val


def _get_pdf_converter(artifacts_path: Optional[str], threads: int, device: str) -> object:
    """PDF 管线转换器懒加载单例；参数变更（配置调整）时在锁内重建。

    参数键 = (artifacts_path, threads, device)：任一变更触发重建，设备切换
    （如 cpu → cuda）不会复用旧设备上的转换器。
    """
    global _pdf_converter, _pdf_converter_params
    device = _normalize_device(device)
    params = (artifacts_path, threads, device)
    if _pdf_converter is not None and _pdf_converter_params == params:
        return _pdf_converter
    with _converter_lock:
        if _pdf_converter is None or _pdf_converter_params != params:
            from docling.datamodel.accelerator_options import AcceleratorOptions
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import (
                PdfPipelineOptions,
                RapidOcrOptions,
                TableFormerMode,
            )
            from docling.document_converter import (
                DocumentConverter,
                PdfFormatOption,
            )

            opts = PdfPipelineOptions()
            # OCR 语义不变量：原生文本恒优先（docling 页级判定），仅无/薄文本层
            # 的扫描页付 OCR 成本；固定 lang=["ch"]（中英数混识），不暴露用户参数
            opts.do_ocr = True
            opts.ocr_options = RapidOcrOptions(lang=["ch"])
            opts.table_structure_options.mode = TableFormerMode.ACCURATE
            opts.table_structure_options.do_cell_matching = True
            opts.generate_page_images = False
            if artifacts_path:
                # 生产禁运行时下载模型：DOCLING_ARTIFACTS_PATH 预置权重
                opts.artifacts_path = Path(artifacts_path)
            _pdf_converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=opts,
                        # device=auto 由 docling 按 torch 探测 cuda→mps→cpu；
                        # num_threads 仅 cpu 生效（GPU 算子不吃该值）
                        accelerator_options=AcceleratorOptions(
                            device=device, num_threads=max(1, threads)
                        ),
                    )
                }
            )
            _pdf_converter_params = params
            logger.info(
                "docling PDF 管线已加载（OCR=zh / TableFormer=ACCURATE / "
                "device=%s / threads=%d）",
                device, threads,
            )
    return _pdf_converter


def _get_docx_converter() -> object:
    """docx 转换器懒加载单例：限定 DOCX 格式（声明式直解，无 ML、无 OCR）。"""
    global _docx_converter
    if _docx_converter is not None:
        return _docx_converter
    with _converter_lock:
        if _docx_converter is None:
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter

            _docx_converter = DocumentConverter(
                allowed_formats=[InputFormat.DOCX]
            )
            logger.info("docling DOCX backend 已加载（声明式直解，无 ML）")
    return _docx_converter


def _convert(converter: object, raw: bytes, shard: Optional[tuple[int, int]]) -> object:
    """单次 convert（串行锁内）：临时文件落盘 + page_range 分片。

    临时文件而非 BytesIO：docling 对流式输入的接受度随版本漂移，文件路径是
    全版本稳定入口。分片只换 page_range，模型与转换器全程复用（评审定版 5）。
    """
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        with _convert_lock:
            if shard is None:
                return converter.convert(path)
            # page_range=(start, end)：1-based 闭区间，须为 tuple（docling 的
            # pydantic 严格校验拒收 list——2.135 实测）
            return converter.convert(path, page_range=(shard[0], shard[1]))
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


# ============================ 对外入口 ======================================


def shard_ranges(total_pages: int, shard_pages: int) -> list[tuple[int, int]]:
    """→ [(start, end)]（1-based 闭区间）。total<=0 → []；shard_pages>=1。"""
    if total_pages <= 0 or shard_pages < 1:
        return []
    out: list[tuple[int, int]] = []
    start = 1
    while start <= total_pages:
        end = min(start + shard_pages - 1, total_pages)
        out.append((start, end))
        start = end + 1
    return out


def pdf_page_count(raw: bytes) -> int:
    """pymupdf 页数探测（分片调度前置；失败 → 0，调用方按整册单分片处理）。"""
    try:
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            return pdf.page_count
    except Exception:
        logger.warning("pdf 页数探测失败（将按整册单分片处理）", exc_info=True)
        return 0


def parse(
    raw: bytes,
    mime: str,
    shard: Optional[tuple[int, int]] = None,
    *,
    artifacts_path: Optional[str] = None,
    threads: int = 4,
    device: str = "auto",
    unit_max_chars: int = 2000,
) -> list[KnowledgeUnit]:
    """解析入口：``parse(file_bytes, mime, shard) → list[KnowledgeUnit]``。

    - pdf：shard 为 (start_page, end_page)（1-based 闭区间；None = 整册，
      调用方负责分片硬约束——review 禁止无 page_range 的 convert 调用）；
      device 透传转换器（auto/cpu/cuda/mps，非法值入口显式拒绝）；
    - docx：声明式直解，shard 忽略（无 OCR、无 anchor，设备不参与）；
    - 预算护栏在本入口内完成（unit_max_chars），产出即最终单元序列。
    """
    if is_pdf(mime):
        converter = _get_pdf_converter(artifacts_path, threads, device)
        rotations = _page_rotations(raw)
        result = _convert(converter, raw, shard)
        units = units_from_document(result.document, shard=shard, rotations=rotations)
    elif is_docx(mime):
        converter = _get_docx_converter()
        result = _convert(converter, raw, None)
        units = units_from_document(result.document, shard=None, rotations={})
    else:
        raise ValueError(f"docling 路径仅支持 pdf/docx，收到 {mime}")

    _strip_unmappable(units)
    units = apply_budget(units, unit_max_chars)
    for i, u in enumerate(units):
        u.unit_index = i
    return units


def _page_rotations(raw: bytes) -> dict[int, int]:
    """pymupdf 独立读取页旋转角（/Rotate）：docling 不透传旋转信息，锚定
    展示侧（PDF.js viewport 自带旋转）需要该元数据随单元下行。"""
    try:
        import pymupdf

        out: dict[int, int] = {}
        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            for i in range(pdf.page_count):
                out[i + 1] = int(pdf[i].rotation or 0)
        return out
    except Exception:
        return {}


# ============================ 单元组装（纯函数，伪 docling 文档可测）=========


def _label_of(item: object) -> str:
    label = getattr(item, "label", None)
    if label is None:
        return ""
    # DocItemLabel enum（.value）与裸字符串双兼容
    return str(getattr(label, "value", label) or "")


def _item_text(item: object) -> str:
    return str(getattr(item, "text", "") or "").strip()


def _first_prov(item: object) -> Optional[tuple[int, object]]:
    """→ (page_no, bbox)。prov 可能是单对象（Text/Table/Picture）或列表（防御）。"""
    prov = getattr(item, "prov", None)
    if prov is None:
        return None
    if isinstance(prov, (list, tuple)):
        if not prov:
            return None
        prov = prov[0]
    page_no = getattr(prov, "page_no", None)
    bbox = getattr(prov, "bbox", None)
    if page_no is None or bbox is None:
        return None
    return int(page_no), bbox


def _normalize_bbox(bbox: object, page_w: float, page_h: float) -> Optional[dict]:
    """docling prov bbox（左下原点 l,b,r,t）→ 归一化左上原点 {x0,y0,x1,y1}。

    clamp 0..1、round 4；宽高非法（<=0）或坐标缺失 → None（单元降级为无锚定，
    不阻断解析）。优先用 bbox 自带的 ``to_top_left_origin``（版本正统路径），
    属性缺失时手工换算（l/b/r/t 属性名跨版本稳定）。
    """
    try:
        if hasattr(bbox, "to_top_left_origin"):
            tl = bbox.to_top_left_origin(page_h)
            l, t, r, b = float(tl.l), float(tl.t), float(tl.r), float(tl.b)
        else:
            l, b, r, t = (
                float(bbox.l), float(bbox.b), float(bbox.r), float(bbox.t)
            )
            t, b = page_h - t, page_h - b  # 左下原点 → 左上原点
        if page_w <= 0 or page_h <= 0:
            return None
        x0, x1 = sorted((l / page_w, r / page_w))
        y0, y1 = sorted((t / page_h, b / page_h))
        out = {}
        for k, v in (("x0", x0), ("y0", y0), ("x1", x1), ("y1", y1)):
            out[k] = round(min(1.0, max(0.0, v)), 4)
        return out
    except (AttributeError, TypeError, ValueError):
        return None


def _table_rows_from_data(data: object) -> list[list[str]]:
    """TableData.grid → rows=[[cell_text, ...], ...]（文本扁平化/切分共用）。

    grid 缺失（API 漂移）→ []，调用方回落 export_to_html / item.text。
    """
    grid = getattr(data, "grid", None)
    if not grid:
        return []
    rows: list[list[str]] = []
    for row in grid:
        cells: list[str] = []
        prev = None
        for cell in row:
            # 跨列单元格在 grid 中按占据位重复出现，同对象去重防文本翻倍
            if cell is prev:
                continue
            prev = cell
            cells.append(str(getattr(cell, "text", "") or "").strip())
        rows.append(cells)
    return rows


def _rows_to_html(rows: list[list[str]], header: Optional[list[str]] = None) -> str:
    """行文本组 → 简单 HTML 表（预算切分兜底路径；宽表拆分重复表头）。"""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts: list[str] = ["<table>"]
    if header:
        parts.append("<thead><tr>")
        parts.extend(f"<th>{esc(c)}</th>" for c in header)
        parts.append("</tr></thead>")
    for row in rows:
        parts.append("<tr>")
        parts.extend(f"<td>{esc(c)}</td>" for c in row)
        parts.append("</tr>")
    parts.append("</table>")
    return "".join(parts)


def _table_html(item: object, doc: object) -> Optional[str]:
    """表格 HTML：优先 docling 官方导出（含 rowspan/colspan 语义），
    API 漂移时回落 grid 手工重建，再回落 None（单元保留文本，仅缺富表格）。"""
    try:
        if hasattr(item, "export_to_html"):
            try:
                out = item.export_to_html(doc)
            except TypeError:  # 版本签名差异：无 doc 入参
                out = item.export_to_html()
            if out and str(out).strip():
                return str(out)
    except Exception:
        logger.debug("table export_to_html 失败，回落 grid 重建", exc_info=True)
    rows = _table_rows_from_data(getattr(item, "data", None))
    if rows:
        return _rows_to_html(rows[1:], header=rows[0])
    return None


def _caption_texts(item: object) -> list[str]:
    """PictureItem.captions/references → 图注文本（容忍 (label, text) 元组、
    裸字符串、带 .text 对象三种形态——docling 版本间表述不一）。"""
    out: list[str] = []
    for attr in ("captions", "references"):
        for entry in getattr(item, attr, None) or []:
            if isinstance(entry, str):
                text = entry
            elif isinstance(entry, (tuple, list)):
                text = str(entry[-1]) if entry else ""
            else:
                text = str(getattr(entry, "text", "") or "")
            if text.strip():
                out.append(text.strip())
    return out


def units_from_document(
    doc: object,
    *,
    shard: Optional[tuple[int, int]] = None,
    rotations: Optional[dict[int, int]] = None,
) -> list[KnowledgeUnit]:
    """DoclingDocument → 知识单元序列（纯函数；伪 docling 文档可驱动测试）。

    - 阅读序标题栈：title/section_header 入栈（有 level 信息时按层级出栈，
      无 level 时截尾保 _MAX_HEADING_DEPTH）；
    - 页号：docling 保留源文档页号；若实现重编号为 1..n（min==1 且分片起点
      非 1），按分片起点偏移回全局页码；
    - picture 单元保留在 IR（不产向量），并作为资产绑定到同页最近的先前
      可嵌入单元（图-图注绑定：caption 优先 docling 绑定，缺失时吸收紧随
      其后的孤立 caption label 项）。
    """
    rotations = rotations or {}
    pages = getattr(doc, "pages", None) or {}

    def _page_size(page_id: int) -> tuple[float, float]:
        page = pages.get(page_id)
        size = getattr(page, "size", None) if page is not None else None
        if size is None:
            return 0.0, 0.0
        return float(getattr(size, "width", 0) or 0), float(
            getattr(size, "height", 0) or 0
        )

    # 分片页号偏移探测（详见 docstring）
    page_offset = 0
    if shard is not None and pages:
        try:
            min_page = min(int(p) for p in pages)
        except (TypeError, ValueError):
            min_page = 1
        if min_page == 1 and shard[0] > 1:
            page_offset = shard[0] - 1

    units: list[KnowledgeUnit] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)
    last_embeddable_by_page: dict[int, int] = {}

    for yielded in doc.iterate_items():
        # 版本兼容：新版产出 item，旧版产出 (item, level) 元组
        item, level = yielded if isinstance(yielded, tuple) else (yielded, None)
        label = _label_of(item)

        if label in _SKIP_LABELS:
            continue

        if label in _HEADING_LABELS:
            text = _item_text(item)
            if text:
                if isinstance(level, int) and level >= 0:
                    lvl = level
                else:
                    # 无 level 信息：单调递增层级 = 只入栈不出栈（append-only），
                    # 深度由 _MAX_HEADING_DEPTH 截尾兜底
                    lvl = (heading_stack[-1][0] + 1) if heading_stack else 0
                while heading_stack and heading_stack[-1][0] >= lvl:
                    heading_stack.pop()
                heading_stack.append((lvl, text))
                del heading_stack[:-_MAX_HEADING_DEPTH]
            continue

        if label == "picture":
            prov = _first_prov(item)
            page = (prov[0] + page_offset) if prov else None
            units.append(
                KnowledgeUnit(
                    kind="picture",
                    text_ctx="；".join(_caption_texts(item)),
                    heading_path=[h for _, h in heading_stack],
                    page=page,
                    page_rotate=rotations.get(page) if page else None,
                    bbox=(
                        _normalize_bbox(prov[1], *_page_size(prov[0]))
                        if prov
                        else None
                    ),
                )
            )
            continue

        if label == "table":
            prov = _first_prov(item)
            page = (prov[0] + page_offset) if prov else None
            rows = _table_rows_from_data(getattr(item, "data", None))
            flat = "\n".join(
                " | ".join(c for c in row if c) for row in rows if any(row)
            )
            units.append(
                KnowledgeUnit(
                    kind="table",
                    text_ctx=flat or _item_text(item),
                    heading_path=[h for _, h in heading_stack],
                    page=page,
                    page_rotate=rotations.get(page) if page else None,
                    bbox=(
                        _normalize_bbox(prov[1], *_page_size(prov[0]))
                        if prov
                        else None
                    ),
                    table_html=_table_html(item, doc),
                )
            )
            if page is not None:
                last_embeddable_by_page[page] = len(units) - 1
            continue

        # 其余文本类（paragraph/list_item/footnote/formula/caption/...）
        text = _item_text(item)
        if not text:
            continue
        if label == "caption":
            # 孤立图注：吸收进阅读序最近的先前 picture 单元（图-图注成对）
            for u in reversed(units):
                if u.kind == "picture":
                    u.text_ctx = f"{u.text_ctx}；{text}" if u.text_ctx else text
                    break
            continue
        prov = _first_prov(item)
        page = (prov[0] + page_offset) if prov else None
        units.append(
            KnowledgeUnit(
                kind="text",
                text_ctx=text,
                heading_path=[h for _, h in heading_stack],
                page=page,
                page_rotate=rotations.get(page) if page else None,
                bbox=(
                    _normalize_bbox(prov[1], *_page_size(prov[0]))
                    if prov
                    else None
                ),
            )
        )
        if page is not None:
            last_embeddable_by_page[page] = len(units) - 1

    _bind_picture_assets(units, last_embeddable_by_page)
    return units


def _bind_picture_assets(
    units: list[KnowledgeUnit], last_embeddable_by_page: dict[int, int]
) -> None:
    """picture 单元 → 资产锚点，绑定到同页最近的先前可嵌入单元。

    绑定语义：图随文走（"见下图"段落携带图的锚点进命中负载）；无同页先行
    可嵌入单元（整页图版）时资产只留在 IR，不进 ES。
    """
    for i, u in enumerate(units):
        if u.kind != "picture":
            continue
        asset = {
            "kind": "picture",
            "page": u.page,
            "bbox": u.bbox,
            "caption": u.text_ctx or None,
            "thumb_b64": None,
        }
        target = last_embeddable_by_page.get(u.page) if u.page is not None else None
        if target is not None and target < i:
            units[target].assets.append(asset)


# ============================ 预算护栏（B1）=================================


#: 不可映射字形清洗：docling 对 PDF 内嵌子集字体缺 ToUnicode 映射的字形输出
#: U+001A（SUBSTITUTE），浏览器渲染为可见乱符号。入库前剥离 C0/DEL 控制符
#: （保留 \n 与 \t），作用于 text_ctx / heading_path / table_html / 表格恢复源。
_UNMAPPABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_unmappable(units: list[KnowledgeUnit]) -> None:
    """就地剥离提取文本中的控制符（parse 出口、apply_budget 之前——
    预算按清洗后的长度计算，与展示/嵌入一致）。"""
    for u in units:
        if u.text_ctx:
            u.text_ctx = _UNMAPPABLE_RE.sub("", u.text_ctx)
        if u.heading_path:
            u.heading_path = [_UNMAPPABLE_RE.sub("", h) for h in u.heading_path]
        if u.table_html:
            u.table_html = _UNMAPPABLE_RE.sub("", u.table_html)


def apply_budget(units: list[KnowledgeUnit], max_chars: int) -> list[KnowledgeUnit]:
    """超预算单元在子区域边界再切（不回盲滑窗）。

    - text：行 → 句 → 硬切（最后手段）三级贪心装箱，切分携带 heading_path；
    - table：表行分组（宽表按行拆分重复表头），从 text_ctx 行结构恢复
      （与 grid 行一一对应）；仅单行表不拆。
    """
    if max_chars < 1:
        return units
    out: list[KnowledgeUnit] = []
    for u in units:
        html_over = u.table_html is not None and len(u.table_html) > max_chars * 2
        if len(u.text_ctx) <= max_chars and not html_over:
            out.append(u)
            continue
        if u.kind == "table":
            out.extend(_split_table(u, max_chars))
        else:
            out.extend(_split_text_unit(u, max_chars))
    return out


def _split_text_unit(u: KnowledgeUnit, max_chars: int) -> list[KnowledgeUnit]:
    parts = _greedy_pack(_split_lines_then_sentences(u.text_ctx, max_chars), max_chars)
    if not parts:  # 全空白（理论不可达）：保留原单元防丢内容
        return [u]
    return [
        KnowledgeUnit(
            kind=u.kind,
            text_ctx=part,
            heading_path=list(u.heading_path),
            page=u.page,
            page_rotate=u.page_rotate,
            bbox=u.bbox,  # 子区域共享父锚点（区域级定位仍成立）
            lang=u.lang,
            doc_uri=u.doc_uri,
            assets=list(u.assets) if i == 0 else [],
        )
        for i, part in enumerate(parts)
    ]


def _split_lines_then_sentences(text: str, max_chars: int) -> list[str]:
    """子区域边界序列：行 →（超预算行内）句 →（超预算句内）硬切。"""
    sentences: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        for s in _SENTENCE_SPLIT_RE.split(line):
            if s:
                sentences.append(s)
    pieces: list[str] = []
    for s in sentences:
        while len(s) > max_chars:  # 无边界连续串（异常 OCR/超长编号）最后手段
            pieces.append(s[:max_chars])
            s = s[max_chars:]
        if s:
            pieces.append(s)
    return pieces


def _greedy_pack(pieces: list[str], max_chars: int) -> list[str]:
    """贪心装箱：相邻片段聚合到不超过 max_chars（分隔符计 1 字符）。"""
    out: list[str] = []
    buf = ""
    for p in pieces:
        if not p.strip():
            continue
        if buf and len(buf) + 1 + len(p) > max_chars:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def _split_table(u: KnowledgeUnit, max_chars: int) -> list[KnowledgeUnit]:
    """表行分组切分：text_ctx 行（"c0 | c1"）与 grid 行一一对应，据此重建
    文本与简单 HTML；首行视为表头，每个分组重复（02 §7.1 宽表拆分语义）。"""
    rows = [
        [c.strip() for c in line.split(" | ")]
        for line in u.text_ctx.split("\n")
        if line.strip()
    ]
    if len(rows) <= 1:
        # 无法恢复行结构（扁平化失败/单行表）：退化为文本级切分
        return _split_text_unit(u, max_chars)
    header, body = rows[0], rows[1:]
    groups: list[list[list[str]]] = []
    cur: list[list[str]] = []
    cur_len = len(" | ".join(header))
    for row in body:
        row_len = len(" | ".join(row)) + 1
        if cur and cur_len + row_len > max_chars:
            groups.append(cur)
            cur, cur_len = [], len(" | ".join(header))
        cur.append(row)
        cur_len += row_len
    if cur:
        groups.append(cur)
    if len(groups) <= 1:
        return [u]
    return [
        KnowledgeUnit(
            kind="table",
            text_ctx="\n".join(
                [" | ".join(c for c in header if c)]
                + [" | ".join(c for c in row if c) for row in g if any(row)]
            ),
            heading_path=list(u.heading_path),
            page=u.page,
            page_rotate=u.page_rotate,
            bbox=u.bbox,
            table_html=_rows_to_html(g, header=header),
            lang=u.lang,
            doc_uri=u.doc_uri,
            assets=list(u.assets) if i == 0 else [],
        )
        for i, g in enumerate(groups)
    ]


# ============================ 缩略图（best-effort）==========================


def render_anchor_thumbs(
    raw: bytes, anchors: list[tuple[int, dict]], *, max_w: int = 200, quality: int = 55
) -> list[Optional[str]]:
    """批量渲染锚点区域缩略图（→ 与 anchors 等长的 b64 列表，失败位 None）。

    实现取"整页低清渲染 + 位图裁剪"而非 clip 直渲：clip 在旋转页上的坐标系
    语义跨 pymupdf 版本不稳，而整页 get_pixmap 恒为展示方向（旋转已应用），
    归一化 bbox 直接按比例裁剪即可（与前端叠框同一几何模型）。
    缩略图仅供命中列表预览；reader/VLM 原图回源走 MinIO 后端（D3），不消费 thumb。
    """
    import base64

    out: list[Optional[str]] = [None] * len(anchors)
    try:
        import pymupdf
    except ImportError:
        return out
    try:
        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            page_pix: dict[int, object] = {}
            for idx, (page_no, bbox) in enumerate(anchors):
                try:
                    if page_no < 1 or page_no > pdf.page_count:
                        continue
                    if page_no not in page_pix:
                        page = pdf[page_no - 1]
                        zoom = max_w / max(1.0, page.rect.width)
                        page_pix[page_no] = page.get_pixmap(
                            matrix=pymupdf.Matrix(zoom, zoom)
                        )
                    pix = page_pix[page_no]
                    w, h = pix.width, pix.height
                    x0, y0 = int(bbox["x0"] * w), int(bbox["y0"] * h)
                    x1, y1 = int(bbox["x1"] * w), int(bbox["y1"] * h)
                    clip = pymupdf.IRect(
                        x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)
                    )
                    if clip.is_empty or clip.is_infinite:
                        continue
                    try:
                        cropped = pymupdf.Pixmap(pix, clip)
                    except TypeError:  # 构造签名差异：(colorspace, source, clip)
                        cropped = pymupdf.Pixmap(pix.colorspace, pix, clip)
                    if cropped.n > 4:
                        cropped = pymupdf.Pixmap(pymupdf.csRGB, cropped)
                    out[idx] = base64.b64encode(
                        cropped.tobytes("jpg", jpg_quality=quality)
                    ).decode("ascii")
                except Exception:  # noqa: S112
                    continue  # 单锚点失败不致命（缩略图是糖）
    except Exception:
        logger.warning("锚点缩略图渲染整体失败（不影响入库）", exc_info=True)
    return out
