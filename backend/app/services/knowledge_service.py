"""知识库领域服务（plan §5.3）：入库 + 文档/库生命周期 + 检索。

职责：
- KB/文档 CRUD（PG 状态卡）+ 入库（parse→embed→ES）+ 检索（ES kNN）。
- chunk 文本/向量真值入 Elasticsearch；PG 仅状态/元数据；MinIO 留底原始字节。
- 双解析引擎（plan: kb-docling-parse-display）：legacy（轻量基线：pymupdf
  文本层+滑窗切块；**无 OCR**——扫描件解析归 docling 独有，职责分工明确）|
  docling（结构单元 IR v1.1 直入 ES，splitter 退役仅预算护栏；分片边车断点
  续跑；pdf 原文件归档 kb-src 产出 doc_uri 证据锚定，docx 直解单元无
  anchor 文本态降级；管线内 RapidOCR 仅扫描页兜底）。
- 维度 per-KB（kb.dims 为取值来源）：创建可设（默认全局）；变更须 force 确认——
  ES mapping 维度不可变 ⇒ drop 索引 + 文档状态重置（UPDATE，不删数据，留底保留，
  用户手动重新解析）。
- 解析手动触发（parse→ingest）：chunk_size/overlap 随解析持久化到 doc 行（生效值），
  reindex 沿用 → 重建可复现；docling 路径忽略滑窗参数（预算护栏 kb_unit_max_chars
  由全局配置供给，双轨语义不串）。

一致性模型（回应「杜绝 PG 持久化但 MinIO/ES 报错悬挂」）：
- 注册文档（register_document）：MinIO 上传在 PG 写入之前 → MinIO 失败即中止
  （不插 PG 行，无悬挂）；MinIO 成功后若 PG 写入失败则回滚删除 MinIO 对象。
- 入库（ingest_document）：异步，幂等（先按 doc_id 删旧 chunk 再写）；任一步失败
  → PG 置 failed（行保留=状态卡，MinIO 留底=可 reindex）。
- 删除（delete_document/delete_kb）：ES→MinIO→PG 顺序，ES/MinIO 失败尽力而为
  （记日志），PG 为「该文档是否存在」的真相源。

显式预检抛类型化异常（FileExistsError→409 / KeyError→404 / ValueError→400 /
KbDocsExistError→409 / DocumentBusyError→409），由路由层映射。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import uuid
from typing import Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.models import db as dbmod
from app.services.mime_utils import is_docx, is_pdf
from app.services.storage import MinioStorage
from app.services.vector_store import VectorStoreFactory

logger = logging.getLogger(__name__)


class DimensionMismatchError(Exception):
    """维度漂移：目标 KB 记录的 dims != ES mapping 真值 → 拦截检索。"""


class KbDocsExistError(Exception):
    """维度变更守卫：库内仍有文档且未 force 确认（路由映射 → 409）。"""

    def __init__(self, doc_count: int):
        self.doc_count = doc_count
        super().__init__(
            f"该库下有 {doc_count} 个文档：修改维度将清空全部索引块并把文档重置为"
            f"待解析（原始文件保留，需重新解析）。确认请携带 force=true 重试"
        )


class DocumentBusyError(Exception):
    """文档正在解析中，拒绝重复投递（路由映射 → 409）。"""


def _embed_progress(done: int, total: int, lo: int = 5) -> int:
    """Map (done/total) embedded batches → lo..99（入库进度百分比）。

    嵌入是耗时主体，解析/收尾快：把已嵌入比例映射到 [lo,99]，留出首尾余量——
    解析完成即将嵌入≈lo%，全部嵌入完≈99%，最终 indexed 固定 100%。
    lo 默认 5（legacy：原生提取毫秒级）；docling 文档解析阶段占 2..50，
    嵌入从 51 起。
    """
    if total <= 0:
        return lo
    return min(99, lo + round(done / total * (99 - lo)))


def _shard_progress(done: int, total: int) -> int:
    """docling 解析阶段进度 → 2..50（片级粒度：解析为耗时主体，嵌入段 51..99）。"""
    if total <= 0:
        return 2
    return min(50, 2 + round(done / total * 48))


# langdetect 确定性（软依赖：未安装 → lang 置 None，不阻断）
try:
    from langdetect import DetectorFactory

    DetectorFactory.seed = 0
except ImportError:  # pragma: no cover - 环境差异
    DetectorFactory = None  # type: ignore[assignment]


def _detect_lang(text: str) -> Optional[str]:
    """语言检测（langdetect 级，C3）：前 400 字符采样；任何异常 → None。"""
    try:
        from langdetect import detect

        lang = detect(text[:400])
        return str(lang) if lang else None
    except Exception:
        return None


class KnowledgeService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        settings: Settings,
        minio: MinioStorage,
        vector_factory: VectorStoreFactory,
    ):
        self.pool = pool
        self.settings = settings
        self.minio = minio
        self.vector_factory = vector_factory

    # ============================ KB CRUD ====================================

    async def create_kb(
        self,
        name: str,
        description: Optional[str],
        creator: str,
        dims: Optional[int] = None,
    ) -> dict:
        if await dbmod.get_kb_by_name(self.pool, name) is not None:
            raise FileExistsError(name)  # -> 409
        new_id = uuid.uuid4()
        return await dbmod.create_kb(
            self.pool,
            name=name,
            description=description,
            es_index_name=f"kb_{new_id}",
            dims=dims if dims is not None else self.settings.embedding_dims,
            creator=creator,
            kb_id=new_id,
        )

    async def update_kb(
        self,
        kb_id: uuid.UUID,
        name: str,
        description: Optional[str],
        dims: Optional[int] = None,
        force: bool = False,
    ) -> dict:
        """更新 KB 元数据；维度变更走守卫（破坏性变更）。

        名称排重镜像模型/智能体更新（排除自身）。维度变更时：
        1. 有文档且未 force → KbDocsExistError（409，前端弹确认）。
        2. 确认变更 → drop_index（mapping 维度不可变，旧向量作废）+
           reset_kb_documents（仅 UPDATE：全部文档→待解析，原始字节与 PG 行保留，
           不删除任何数据）+ 落库新 dims。用户随后按新维度逐个重新解析。
        3. 空库 → 直接改（drop_index 幂等，清可能的残留索引）。

        已知边界：若变更时有该库文档正在解析中（in-flight ingest），旧维度客户端
        可能把 chunk 写入重建后的索引造成维度错位——属管理面并发操作，重新解析
        该文档即可修复（ingest 幂等删旧）。
        """
        existing = await dbmod.get_kb(self.pool, kb_id)
        if existing is None:
            raise KeyError(f"kb {kb_id} not found")  # -> 404
        # 改名时校验目标 name 未被其它 KB 占用（index_name 不可变，禁止改）
        if name != existing["name"]:
            clash = await dbmod.get_kb_by_name(self.pool, name)
            if clash is not None:
                raise FileExistsError(name)  # -> 409

        effective_dims = dims if dims is not None else existing["dims"]
        dims_changed = effective_dims != existing["dims"]
        if dims_changed:
            docs = await dbmod.list_kb_documents(self.pool, kb_id)
            if docs and not force:
                raise KbDocsExistError(len(docs))  # -> 409（前端二次确认）
            # 旧维度向量全部作废：drop 索引（幂等）+ 文档重置为待解析（UPDATE 保留数据）
            self.vector_factory.drop_index(str(kb_id))
            if docs:
                reset = await dbmod.reset_kb_documents(self.pool, kb_id)
                logger.info(
                    "kb=%s dims %s→%s：索引已重建基底，%d 个文档重置为待解析",
                    kb_id, existing["dims"], effective_dims, reset,
                )

        return await dbmod.update_kb(
            self.pool, kb_id, name, description,
            dims=effective_dims if dims_changed else None,  # 未变更传 None 保持原值
        )

    async def get_kb(self, kb_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.get_kb(self.pool, kb_id)

    async def list_kbs(self) -> list[dict]:
        return await dbmod.list_kbs(self.pool)

    async def delete_kb(self, kb_id: uuid.UUID) -> None:
        existing = await dbmod.get_kb(self.pool, kb_id)
        if existing is None:
            raise KeyError(f"kb {kb_id} not found")  # -> 404
        # ES：删索引（全部 chunk）；MinIO：删该库全部原始字节（含边车/分片 IR/
        # kb-src 归档）；PG：CASCADE
        self.vector_factory.drop_index(str(kb_id))
        docs = await dbmod.list_kb_documents(self.pool, kb_id)
        for d in docs:
            if d.get("bucket_key"):
                self.minio.remove_object(self._sidecar_key(d["bucket_key"]))
                self.minio.remove_prefix(f"{d['bucket_key']}.shards/")
                self.minio.remove_prefix(f"kb-src/{d['id']}/")
                self.minio.remove_object(d["bucket_key"])
        await dbmod.delete_kb(self.pool, kb_id)

    # ============================ Documents =================================

    async def register_document(
        self, kb_id: uuid.UUID, filename: str, mime: str, raw: bytes
    ) -> dict:
        """注册文档（pending=待解析）：MinIO 在前、PG 在后（一致性，杜绝悬挂）。

        KB 仅收 pdf/docx；原始字节必须留底 MinIO（延迟手动解析依赖留底取字节，
        KB_STORE_MINIO=false 由路由层门控拒绝）。同名拦截：同 (kb_id, filename)
        已有文档则直接拒绝（FileExistsError -> 409），不执行 MinIO 上传。并发竞态
        （check 与 insert 之间）由 DB 唯一索引兜住，仍归一为同名冲突。
        """
        if await dbmod.get_kb(self.pool, kb_id) is None:
            raise KeyError(f"kb {kb_id} not found")  # -> 404
        if not (is_pdf(mime) or is_docx(mime)):
            raise ValueError(f"知识库仅支持 pdf/docx，收到 {mime}")  # -> 400
        # 同名预检：精确匹配 filename（区分大小写）；命中即拒绝，MinIO 不上传
        if await dbmod.get_kb_document_by_name(self.pool, kb_id, filename) is not None:
            raise FileExistsError(filename)  # -> 409

        from app.services.mime_utils import extract_document_meta

        page_count, _ = extract_document_meta(raw, mime)

        new_id = uuid.uuid4()
        bucket_key: Optional[str] = None
        if self.settings.kb_store_minio:
            bucket_key = f"kb/{kb_id}/{new_id}"
            # ① MinIO 先行：失败即中止，不插 PG 行（无悬挂）
            try:
                self.minio.put_object(bucket_key, raw, content_type=mime)
            except Exception as e:
                logger.exception("MinIO 存储失败 (kb=%s)", kb_id)
                raise RuntimeError(f"MinIO 存储失败: {e}") from e

        # ② PG 状态卡（pending=待解析）；失败则回滚清 MinIO，避免悬挂字节
        try:
            return await dbmod.create_kb_document(
                self.pool,
                kb_id=kb_id,
                bucket_key=bucket_key,
                filename=filename,
                mime=mime,
                size=len(raw),
                page_count=page_count,
                doc_id=new_id,
            )
        except Exception:
            if bucket_key:
                self.minio.remove_object(bucket_key)
            raise

    async def list_documents(self, kb_id: uuid.UUID) -> list[dict]:
        return await dbmod.list_kb_documents(self.pool, kb_id)

    async def get_document(self, doc_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.get_kb_document(self.pool, doc_id)

    async def delete_document(self, kb_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        doc = await dbmod.get_kb_document(self.pool, doc_id)
        if doc is None or str(doc["kb_id"]) != str(kb_id):
            raise KeyError(f"doc {doc_id} not found in kb {kb_id}")  # -> 404
        # ES→MinIO→PG：ES/MinIO 尽力而为，PG 为存在性真相
        self.vector_factory.delete_by_doc_id(str(kb_id), str(doc_id))
        if doc.get("bucket_key"):
            self.minio.remove_object(self._sidecar_key(doc["bucket_key"]))
            self.minio.remove_prefix(f"{doc['bucket_key']}.shards/")
            self.minio.remove_prefix(f"kb-src/{doc_id}/")
            self.minio.remove_object(doc["bucket_key"])
        await dbmod.delete_kb_document(self.pool, doc_id)

    async def parse_document(
        self,
        kb_id: uuid.UUID,
        doc_id: uuid.UUID,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> dict:
        """手动解析触发（前置校验 + 参数持久化；投递入库由路由层 ingest_pool）。

        参数解析优先级：显式传入 > doc 行已存值（重新解析沿用）> 全局默认。
        首次解析时 doc 行尚无参数（NULL），显式传入或默认值即生效值并落库。
        """
        doc = await dbmod.get_kb_document(self.pool, doc_id)
        if doc is None or str(doc["kb_id"]) != str(kb_id):
            raise KeyError(f"doc {doc_id} not found in kb {kb_id}")  # -> 404
        if doc["status"] == "indexing":
            raise DocumentBusyError(f"文档「{doc['filename']}」正在解析中")  # -> 409
        if not doc.get("bucket_key"):
            raise ValueError("无 MinIO 留底（历史文档），无法解析；请删除后重新上传")  # -> 400

        eff_size = chunk_size if chunk_size is not None else (
            doc.get("chunk_size") or self.settings.kb_chunk_size
        )
        eff_overlap = chunk_overlap if chunk_overlap is not None else (
            doc.get("chunk_overlap") or self.settings.kb_chunk_overlap
        )
        if not (0 <= eff_overlap < eff_size):
            raise ValueError(
                f"切块参数非法：需 0 ≤ overlap({eff_overlap}) < chunk_size({eff_size})"
            )  # -> 400
        return await dbmod.update_kb_document_parse_params(
            self.pool, doc_id, eff_size, eff_overlap
        )

    async def reindex_document(self, kb_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        """重建索引：从 MinIO 留底重新 ingest（沿用已存解析参数；幂等删旧 chunk）。"""
        doc = await dbmod.get_kb_document(self.pool, doc_id)
        if doc is None or str(doc["kb_id"]) != str(kb_id):
            raise KeyError(f"doc {doc_id} not found in kb {kb_id}")  # -> 404
        if not doc.get("bucket_key"):
            raise ValueError("未保留原始字节（KB_STORE_MINIO=false），无法重建；请重新上传")  # -> 400
        await self.ingest_document(kb_id, doc_id)

    async def ingest_document(
        self, kb_id: uuid.UUID, doc_id: uuid.UUID, raw_bytes: Optional[bytes] = None
    ) -> None:
        """入库：pending→indexing→indexed|failed（幂等：先删旧 chunk）。

        - 嵌入维度 = kb.dims（随库行走；None=全局默认）→ store/嵌入客户端按维度构建。
        - 引擎分派（C1）：``kb_parser_engine=legacy``（默认）走 pymupdf+滑窗
          （现状零改动）；``docling`` 走结构单元直入 ES（splitter 退役，仅预算
          护栏，见 _ingest_docling）。
        - raw_bytes：兼容旧内存路径；手动解析流程恒为 None（从 MinIO 取）。
        - 进度：按 embedding_batch_size 分批嵌入，每批后回写 progress（0..100），
          前端经文档列表轮询渲染进度条。
        """
        await dbmod.update_kb_document_status(
            self.pool, doc_id, status="indexing", progress=0
        )
        try:
            doc = await dbmod.get_kb_document(self.pool, doc_id)
            if doc is None:
                raise KeyError(f"doc {doc_id} vanished during ingest")
            kb = await dbmod.get_kb(self.pool, kb_id)
            if kb is None:
                raise KeyError(f"kb {kb_id} vanished during ingest")
            dims = kb.get("dims")

            raw = raw_bytes
            if raw is None:
                if not doc.get("bucket_key"):
                    raise ValueError("无原始字节可入库（既未透传内存，也无 MinIO 留底）")
                raw = await self._read_bytes_async(doc["bucket_key"])

            if self.settings.kb_parser_engine == "docling":
                await self._ingest_docling(kb_id, doc_id, doc, dims, raw)
            else:
                await self._ingest_legacy(kb_id, doc_id, doc, dims, raw)
        except Exception as e:
            logger.exception("ingest failed doc=%s kb=%s", doc_id, kb_id)
            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="failed", error=str(e)[:500]
            )

    async def _ingest_legacy(
        self,
        kb_id: uuid.UUID,
        doc_id: uuid.UUID,
        doc: dict,
        dims: Optional[int],
        raw: bytes,
    ) -> None:
        """legacy 入库路径（轻量基线）：pymupdf 文本层提取 + 滑窗切块
        （RecursiveCharacterTextSplitter 仅此路径保留）。无 OCR——扫描件
        （无文本层）产出空页，由 _empty_reason 报错指引 docling 引擎。"""
        # 生效参数：doc 行优先，NULL（历史行）回落全局默认并回写（保证可复现）
        eff_size = doc.get("chunk_size") or self.settings.kb_chunk_size
        eff_overlap = doc.get("chunk_overlap") or self.settings.kb_chunk_overlap
        if doc.get("chunk_size") is None:
            await dbmod.update_kb_document_parse_params(
                self.pool, doc_id, eff_size, eff_overlap
            )

        # 提取（pdf 逐页文本层 / docx 整篇单页，毫秒级同步直跑）→ 切块
        pages = self._extract_pages(raw, doc["mime"])
        chunks = self._split_pages(
            pages, str(kb_id), str(doc_id), doc["filename"], doc["mime"],
            chunk_size=eff_size, chunk_overlap=eff_overlap,
        )
        if not chunks:
            raise ValueError(self._empty_reason(doc["mime"], pages))

        await self._write_chunks(
            kb_id, doc_id, dims, chunks,
            lo=5, page_count=doc.get("page_count"),
            parser_engine="legacy",  # 显式回写：防 docling→legacy 重解析后标记悬挂
        )
        logger.info(
            "ingested(legacy) doc=%s chunks=%d kb=%s dims=%s chunk=%d/%d",
            doc_id, len(chunks), kb_id, dims, eff_size, eff_overlap,
        )

    async def _write_chunks(
        self,
        kb_id: uuid.UUID,
        doc_id: uuid.UUID,
        dims: Optional[int],
        chunks: list[Document],
        *,
        lo: int = 5,
        chunk_total: Optional[int] = None,
        page_count: Optional[int] = None,
        parser_engine: Optional[str] = None,
    ) -> None:
        """分批嵌入 + ES 写入 + 进度/终态回写（两引擎共用尾部）。

        幂等基座：先删该文档旧 chunk（应对重传/重试/重建；新块数少于旧块时
        清残留）。progress 粒度 = 单批（单批耗时随硬件而定）。
        """
        total = chunk_total if chunk_total is not None else len(chunks)
        store = self.vector_factory.get(str(kb_id), dims)
        self.vector_factory.delete_by_doc_id(str(kb_id), str(doc_id))

        ensured = False
        if parser_engine == "docling":
            # 既有索引 live 增列（B3）；langchain 首批建索引的场景在首批后补调
            self.vector_factory.ensure_docling_fields(str(kb_id))
            ensured = True

        batch_size = max(1, self.settings.embedding_batch_size)
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            # _id 必须取 chunk 自带 chunk_index，而非列表位置：docling 的
            # unit_index 在预算切分后重编号（图片/空单元被滤除致位置与序号
            # 错位）。曾按位置生成 _id，anchor_cards 以 {doc_id}_{chunk_index}
            # 确定性查询 miss → 锚定卡全量 null、命中降级纯文本行。legacy
            # 两个值恒等不受影响；chunk_index 缺失回落位置序（防御未来引擎）。
            batch_ids = [
                f"{doc_id}_{c.metadata.get('chunk_index', i)}"
                for i, c in enumerate(batch, start=start)
            ]
            store.add_documents(batch, ids=batch_ids)
            if parser_engine == "docling" and not ensured:
                self.vector_factory.ensure_docling_fields(str(kb_id))
                ensured = True
            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="indexing",
                progress=_embed_progress(start + len(batch), total, lo=lo),
            )

        await dbmod.update_kb_document_status(
            self.pool, doc_id, status="indexed", chunk_count=total,
            page_count=page_count, parser_engine=parser_engine, progress=100,
        )

    # ============================ docling 路径（C1-C4）========================

    # v3：边车校验新增 device（kb_docling_device）——设备切换（cpu↔cuda）会
    # 带来 OCR/布局输出细微差异，参数不一致即判未命中重跑，维持"重建可复现"。
    _SHARD_SIDECAR_VERSION = 3

    @staticmethod
    def _shard_sidecar_key(bucket_key: str, shard_index: int) -> str:
        return f"{bucket_key}.shards/{shard_index}.ir.json"

    @staticmethod
    def _kb_src_key(doc_id: uuid.UUID, doc_version: int, filename: str) -> str:
        """C4 原文件归档键：``kb-src/{doc_id}/v{doc_version}/{filename}.pdf``
        （doc_uri 契约——仅 pdf 产出；文件名剥路径分隔符防键注入）。"""
        safe = (
            (filename or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1].strip()
            or "document.pdf"
        )
        return f"kb-src/{doc_id}/v{max(1, int(doc_version))}/{safe}"

    async def _ingest_docling(
        self,
        kb_id: uuid.UUID,
        doc_id: uuid.UUID,
        doc: dict,
        dims: Optional[int],
        raw: bytes,
    ) -> None:
        """docling 入库路径（C1-C4）：分片解析 → 结构单元直入 ES。

        - 分片调度（C2）：片 = 独立 convert + 独立 IR 边车
          （{bucket_key}.shards/{n}.ir.json，形状/参数不匹配视为未命中）；
          片级断点：中途 kill worker 后重试只补缺失片（既有纪律平移）。
        - docx：声明式直解（无 shard、无 OCR、无 anchor，doc_uri 置空）。
        - 进度：解析片 i/N → 2..50；嵌入 → 51..99（docling 解析为耗时主体）。
        - embed 输入 = heading_path + "\\n" + text_ctx（语境前缀；ES 存原文
          text_ctx，检索出口回放原文——reader 消费不掺面包屑）。
        """
        from app.services import docling_parser

        if not docling_parser.docling_available():
            raise RuntimeError(
                "kb_parser_engine=docling 但未安装 docling"
                "（pip install 'docling>=2.130,<3'），或将 KB_PARSER_ENGINE 切回 legacy"
            )

        st = self.settings
        pdf_doc = is_pdf(doc["mime"])

        page_count: Optional[int] = None
        shards: list[Optional[tuple[int, int]]]
        if pdf_doc:
            page_count = await asyncio.to_thread(docling_parser.pdf_page_count, raw)
            page_count = page_count or doc.get("page_count") or 0
            if page_count <= 0:
                raise ValueError("无法读取 PDF 页数（文件损坏？），无法分片解析")
            shards = docling_parser.shard_ranges(
                page_count, st.kb_docling_shard_pages
            )
        else:
            shards = [None]  # docx：整篇单次直解

        units: list[docling_parser.KnowledgeUnit] = []
        n_shards = len(shards)
        for i, shard in enumerate(shards):
            units_i: Optional[list[docling_parser.KnowledgeUnit]] = None
            if doc.get("bucket_key"):
                units_i = await self._load_shard_sidecar(
                    doc["bucket_key"], i, shard, st.kb_unit_max_chars,
                    st.kb_docling_device,
                )
                if units_i is not None:
                    logger.info(
                        "doc=%s 命中分片 IR 缓存（%d/%d，%d 单元）",
                        doc_id, i + 1, n_shards, len(units_i),
                    )
            if units_i is None:
                units_i = await asyncio.to_thread(
                    docling_parser.parse,
                    raw, doc["mime"], shard,
                    artifacts_path=st.kb_docling_artifacts_path or None,
                    threads=st.kb_docling_threads,
                    device=st.kb_docling_device,
                    unit_max_chars=st.kb_unit_max_chars,
                )
                if pdf_doc and units_i:
                    await asyncio.to_thread(self._attach_thumbs, raw, units_i)
                if doc.get("bucket_key") and units_i:
                    await self._save_shard_sidecar(
                        doc["bucket_key"], i, shard, st.kb_unit_max_chars,
                        st.kb_docling_device, units_i,
                    )
            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="indexing",
                progress=_shard_progress(i + 1, n_shards),
            )
            units.extend(units_i)

        embeddable = [
            u for u in units if u.kind in ("text", "table") and u.text_ctx.strip()
        ]
        if not embeddable:
            raise ValueError(
                "解析后无可索引文本单元（空文档、解析失败或文档仅含图片资产）"
            )

        # C4 原文件归档（doc_uri 契约：仅 pdf）。失败降级为无锚定命中（文本
        # 卡片），不阻断入库——锚定是增强，不是正确性依赖。
        doc_uri: Optional[str] = None
        if pdf_doc:
            doc_uri = self._kb_src_key(
                doc_id, doc.get("doc_version") or 1, doc["filename"]
            )
            try:
                await asyncio.to_thread(
                    self.minio.put_object, doc_uri, raw, "application/pdf"
                )
            except Exception:
                logger.warning("kb-src 原文件归档失败（命中将降级为文本卡片）", exc_info=True)
                doc_uri = None

        chunks: list[Document] = []
        for u in embeddable:
            u.doc_uri = doc_uri
            u.lang = _detect_lang(u.text_ctx)
            embed_text = (
                " > ".join(u.heading_path) + "\n" + u.text_ctx
                if u.heading_path
                else u.text_ctx
            )
            chunks.append(
                Document(
                    page_content=embed_text,
                    metadata={
                        "kb_id": str(kb_id),
                        "doc_id": str(doc_id),
                        "filename": doc["filename"],
                        "mime": doc["mime"],
                        "chunk_index": u.unit_index,
                        "page": u.page,
                        "kind": u.kind,
                        "heading_path": u.heading_path or None,
                        "text_ctx": u.text_ctx,
                        "lang": u.lang,
                        "doc_uri": u.doc_uri,
                        "page_rotate": u.page_rotate,
                        "bbox": u.bbox,
                        "assets": u.assets or None,
                    },
                )
            )

        await self._write_chunks(
            kb_id, doc_id, dims, chunks,
            lo=51, chunk_total=len(embeddable),
            page_count=page_count or doc.get("page_count"),
            parser_engine="docling",
        )
        logger.info(
            "ingested(docling) doc=%s units=%d(embed=%d) kb=%s shards=%d max_chars=%d",
            doc_id, len(units), len(embeddable), kb_id, n_shards, st.kb_unit_max_chars,
        )

    @staticmethod
    def _attach_thumbs(raw: bytes, units: list) -> None:
        """资产缩略图（best-effort）：绑定资产的锚点区域低清渲染入 IR
        （随边车缓存；仅命中列表预览用——VLM/reader 原图回源走 MinIO，D3）。"""
        from app.services import docling_parser

        anchors: list[tuple[int, dict]] = []
        slots: list[tuple[int, int]] = []
        for ui, u in enumerate(units):
            for ai, a in enumerate(u.assets):
                if isinstance(a.get("bbox"), dict) and a.get("page"):
                    anchors.append((a["page"], a["bbox"]))
                    slots.append((ui, ai))
        if not anchors:
            return
        thumbs = docling_parser.render_anchor_thumbs(raw, anchors)
        for (ui, ai), thumb in zip(slots, thumbs):
            if thumb:
                units[ui].assets[ai]["thumb_b64"] = thumb

    async def _load_shard_sidecar(
        self,
        bucket_key: str,
        shard_index: int,
        shard: Optional[tuple[int, int]],
        unit_max_chars: int,
        device: str,
    ) -> Optional[list]:
        """读分片 IR 边车；形状/参数不匹配（版本升级、预算/设备调整、分片重排）
        一律视为未命中（既有纪律），绝不带病复活缓存。"""
        from app.services import docling_parser

        def _read() -> Optional[dict]:
            key = self._shard_sidecar_key(bucket_key, shard_index)
            if not self.minio.object_exists(key):
                return None
            try:
                data = json.loads(self.minio.get_object(key))
                return data if isinstance(data, dict) else None
            except Exception:
                return None

        data = await asyncio.to_thread(_read)
        if data is None:
            return None
        if (
            data.get("version") != self._SHARD_SIDECAR_VERSION
            or data.get("engine") != "docling"
            or data.get("ir_version") != docling_parser.IR_VERSION
            or data.get("unit_max_chars") != unit_max_chars
            or data.get("device") != device
            or data.get("shard") != (list(shard) if shard else None)
        ):
            return None
        return docling_parser.units_from_payload(data.get("units"))

    async def _save_shard_sidecar(
        self,
        bucket_key: str,
        shard_index: int,
        shard: Optional[tuple[int, int]],
        unit_max_chars: int,
        device: str,
        units: list,
    ) -> None:
        from app.services import docling_parser

        payload = json.dumps(
            {
                "version": self._SHARD_SIDECAR_VERSION,
                "engine": "docling",
                "ir_version": docling_parser.IR_VERSION,
                "shard": list(shard) if shard else None,
                "unit_max_chars": unit_max_chars,
                "device": device,
                "units": docling_parser.units_to_payload(units),
            },
            ensure_ascii=False,
        ).encode("utf-8")

        def _write() -> None:
            self.minio.put_object(
                self._shard_sidecar_key(bucket_key, shard_index),
                payload,
                content_type="application/json",
            )

        try:
            await asyncio.to_thread(_write)
        except Exception:
            logger.warning(
                "分片 IR 边车写入失败（不影响入库，重解析将重跑该分片）", exc_info=True
            )

    # ============================ Search ====================================

    async def search(
        self,
        kb_ids: Optional[list[uuid.UUID]],
        query: str,
        k: Optional[int] = None,
        doc_ids: Optional[list[uuid.UUID]] = None,
    ) -> list[dict]:
        """检索 top-k：[{content,score,filename,doc_id,page,chunk_index,kb_id}]。

        - kb_ids 必填（`kb_*` 通配已在多维度下移除：各库维度不再恒等，通配查询
          无法确定查询向量维度）；空列表直接返回 []。
        - 按 kb.dims 分组：同组一次 kNN（查询向量与组内 mapping 同维）；跨组分数
          不可比 → 轮转归并（round-robin），禁止裸分数排序。
        - 维度漂移检测：kb.dims vs ES mapping 真值（get_dims）→ DimensionMismatchError。
        - doc_ids 非空 → 叠加 metadata.doc_id.keyword terms 过滤。
        """
        k = k or self.settings.kb_top_k
        if not kb_ids:
            return []

        kbs = await dbmod.get_kbs_by_ids(self.pool, kb_ids)
        if not kbs:
            return []

        # 维度真值探测（一次一库；结果复用于漂移检测 + 空库过滤 + 分组）。
        # 未建索引（上传未解析/空库）→ 无 chunk 无命中，跳过而非报错：
        # 未解析属用户未执行操作，非检索链路故障。
        mapping_dims_by_kb: dict[str, Optional[int]] = {}
        for kb in kbs:
            md = self.vector_factory.get_dims(str(kb["id"]))
            mapping_dims_by_kb[str(kb["id"])] = md
            # 漂移检测：PG 记录 vs ES mapping 真值（任一为 None 则无法比对，跳过）
            if (
                md is not None
                and kb.get("dims") is not None
                and md != kb["dims"]
            ):
                raise DimensionMismatchError(
                    f"知识库 '{kb['name']}' 维度 {kb['dims']} 与索引实际维度 "
                    f"{md} 不一致，请重新解析该库文档"
                )

        indexable = [kb for kb in kbs if mapping_dims_by_kb[str(kb["id"])] is not None]
        if not indexable:
            return []

        # 按 mapping 真值分组（查询向量维度必须匹配索引；漂移检测已保证 == kb.dims）
        groups: dict[Optional[int], list[dict]] = {}
        for kb in indexable:
            groups.setdefault(mapping_dims_by_kb[str(kb["id"])], []).append(kb)

        flt: list[dict] = []
        if doc_ids:
            flt.append({"terms": {"metadata.doc_id.keyword": [str(d) for d in doc_ids]}})

        k_per_group = max(1, math.ceil(k / len(groups)))
        group_hits: list[list[tuple[Document, float]]] = []
        for dims, group in groups.items():
            index = ",".join(f"kb_{kb['id']}" for kb in group)
            store = self.vector_factory.get_for_index(index, dims)
            group_hits.append(
                store.similarity_search_with_score(query, k=k_per_group, filter=flt or None)
            )

        # 轮转归并：各组按分数降序，逐位交错取用后截断到 k（跨组分数不可比）
        merged: list[tuple[Document, float]] = []
        for i in range(k_per_group):
            for hits in group_hits:
                if i < len(hits):
                    merged.append(hits[i])
        merged = merged[:k]

        results: list[dict] = []
        # doc_uri → 预签名 URL（D1）：同文档命中共享一次签名；失败降级为
        # 仅 doc_uri（前端不出 viewer 入口）。legacy/无 anchor 命中全 None——
        # B2 可空契约：存在即锚定、缺失即文本态降级。
        presigned: dict[str, str] = {}
        for d, score in merged:
            md = d.metadata or {}
            doc_uri = md.get("doc_uri")
            doc_url: Optional[str] = None
            if doc_uri:
                if doc_uri not in presigned:
                    try:
                        presigned[doc_uri] = self.minio.presign_get(doc_uri)
                    except Exception:
                        logger.warning("doc_uri 预签名失败 %s", doc_uri, exc_info=True)
                        presigned[doc_uri] = ""
                doc_url = presigned[doc_uri] or None
            results.append(
                {
                    "content": md.get("text_ctx") or d.page_content,
                    "score": float(score),
                    "filename": md.get("filename", ""),
                    "doc_id": md.get("doc_id"),
                    "page": md.get("page"),
                    "chunk_index": md.get("chunk_index"),
                    "kb_id": md.get("kb_id"),
                    "kind": md.get("kind"),
                    "heading_path": md.get("heading_path"),
                    "page_rotate": md.get("page_rotate"),
                    "doc_uri": doc_uri,
                    "doc_url": doc_url,
                    "bbox": md.get("bbox"),
                    "assets": md.get("assets"),
                }
            )
        return results

    # ============================ 锚定卡（聊天侧补全）=========================

    async def anchor_cards(self, items: list[dict]) -> list[Optional[dict]]:
        """kb_search 工具结果的锚定补全（聊天侧渲染通道）。

        输入 = 工具结果 hits 的键组（kb_id/doc_id/chunk_index，确定性 chunk id
        ``{doc_id}_{chunk_index}`` 两引擎同构）；输出与输入等长对位，缺失/无锚
        的位为 None（legacy / docx 直解命中降级为纯文本行）。

        与 mcp-servers 解耦的关键：kb_search 面向 LLM 只回文本；锚定元数据
        （bbox/assets/thumb）由前端持 hits 键组来 backend 懒取——本进程持有
        MinIO（doc_uri → 预签名），且懒签规避工具结果持久化后 URL 过期。
        """
        presigned: dict[str, str] = {}
        out: list[Optional[dict]] = []
        for item in items:
            kb_id = str(item["kb_id"])
            chunk_id = f"{item['doc_id']}_{item['chunk_index']}"
            source = await asyncio.to_thread(
                self.vector_factory.get_chunk_source, kb_id, chunk_id
            )
            if source is None:
                out.append(None)
                continue
            md = source.get("metadata") or {}
            doc_uri = md.get("doc_uri")
            if doc_uri and doc_uri not in presigned:
                try:
                    presigned[doc_uri] = self.minio.presign_get(doc_uri)
                except Exception:
                    logger.warning("doc_uri 预签名失败 %s", doc_uri, exc_info=True)
                    presigned[doc_uri] = ""
            out.append(
                {
                    "content": md.get("text_ctx") or source.get("text", ""),
                    "filename": md.get("filename", ""),
                    "heading_path": md.get("heading_path"),
                    "page": md.get("page"),
                    "page_rotate": md.get("page_rotate"),
                    "kind": md.get("kind"),
                    "doc_uri": doc_uri,
                    "doc_url": (presigned.get(doc_uri) or None) if doc_uri else None,
                    "bbox": md.get("bbox"),
                    "assets": md.get("assets"),
                }
            )
        return out

    # ============================ Parsing helpers ===========================

    # （历史注记）legacy 曾有 pdf_ocr 扫描页 OCR 兜底 + {bucket_key}.pages.json
    # 结果边车（dpi/min_chars 键控）。已随「扫描件解析归 docling 独有」的分工
    # 一并退役：legacy 只做原生文本层提取，扫描件经 _empty_reason 报错指引
    # 切换 docling 引擎。MinIO 中的存量 .pages.json 对象成为无害遗留，可另行
    # 人工清理。docling 侧的 IR 分片边车（.shards/*.ir.json）不受影响。

    @staticmethod
    def _empty_reason(mime: str, pages: list[tuple[int, str]]) -> str:
        """空文档报错文案：区分"扫描件"（可行动：切 docling 引擎重解析）
        与一般空文档。"""
        if is_pdf(mime) and pages and not any(t.strip() for _, t in pages):
            return (
                "解析后无可索引文本：PDF 为无文本层扫描件——legacy 引擎不支持"
                " OCR（扫描件解析为 docling 引擎专属能力），请将部署配置"
                " KB_PARSER_ENGINE=docling 后对该文档重新解析"
            )
        return "解析后无可索引文本（空文档或解析失败）"

    def _split_pages(
        self,
        pages: list[tuple[int, str]],
        kb_id: str,
        doc_id: str,
        filename: str,
        mime: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[Document]:
        """切块（按文档生效参数构造 splitter）→ 带 metadata。

        RecursiveCharacterTextSplitter 无状态且构造廉价，按参即时构造不缓存。
        pages 由 _extract_pages 产出（pdf 逐页 / docx 整篇单页）；
        空页在此统一过滤。
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        chunks: list[Document] = []
        for page_no, text in pages:
            if not text.strip():
                continue
            for ci, piece in enumerate(splitter.split_text(text)):
                chunks.append(
                    Document(
                        page_content=piece,
                        metadata={
                            "kb_id": kb_id,
                            "doc_id": doc_id,
                            "filename": filename,
                            "mime": mime,
                            "chunk_index": ci,
                            "page": page_no,
                        },
                    )
                )
        return chunks

    @staticmethod
    def _extract_pages(raw: bytes, mime: str) -> list[tuple[int, str]]:
        """→ [(page_no, text)]。原生提取（pdf 逐页文本层 / docx 整篇单页）。

        pdf 无文本层（扫描件）时逐页为空——legacy 不做 OCR，由上层
        _empty_reason 报错指引切换 docling 引擎（扫描件解析为其专属能力）。
        """
        if is_pdf(mime):
            try:
                import fitz  # pymupdf

                out: list[tuple[int, str]] = []
                with fitz.open(stream=raw, filetype="pdf") as pdf:
                    for i in range(pdf.page_count):
                        out.append((i + 1, pdf[i].get_text("text")))
                return out
            except Exception:
                logger.warning("pdf 解析失败，将作空文档处理")
                return []
        if is_docx(mime):
            try:
                from docx import Document

                doc = Document(io.BytesIO(raw))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                return [(1, text)] if text else []
            except Exception:
                logger.warning("docx 解析失败，将作空文档处理")
                return []
        return []

    async def _read_bytes_async(self, bucket_key: str) -> bytes:
        """MinIO 取字节（同步客户端，丢线程池避免阻塞事件循环）。"""
        return await asyncio.to_thread(self.minio.get_object, bucket_key)
