"""知识库领域服务（plan §5.3）：入库 + 文档/库生命周期 + 检索。

职责：
- KB/文档 CRUD（PG 状态卡）+ 入库（parse→split→embed→ES）+ 检索（ES kNN）。
- chunk 文本/向量真值入 Elasticsearch；PG 仅状态/元数据；MinIO 留底原始字节。
- 维度 per-KB（kb.dims 为取值来源）：创建可设（默认全局）；变更须 force 确认——
  ES mapping 维度不可变 ⇒ drop 索引 + 文档状态重置（UPDATE，不删数据，留底保留，
  用户手动重新解析）。
- 解析手动触发（parse→ingest）：chunk_size/overlap 随解析持久化到 doc 行（生效值），
  reindex 沿用 → 重建可复现。

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

import io
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


def _embed_progress(done: int, total: int) -> int:
    """Map (done/total) embedded batches → 5..99（入库进度百分比）。

    嵌入是耗时主体，解析/收尾快：把已嵌入比例映射到 [5,99]，留出首尾余量——
    解析完成即将嵌入≈5%，全部嵌入完≈99%，最终 indexed 固定 100%。
    """
    if total <= 0:
        return 5
    return min(99, 5 + round(done / total * 94))


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
        # ES：删索引（全部 chunk）；MinIO：删该库全部原始字节；PG：CASCADE 删库+文档行
        self.vector_factory.drop_index(str(kb_id))
        docs = await dbmod.list_kb_documents(self.pool, kb_id)
        for d in docs:
            if d.get("bucket_key"):
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
            except Exception as e:  # noqa: BLE001
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
        - 切块参数 = doc 行生效值（解析时持久化；NULL 历史行回落全局默认并回写）。
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

            # 生效参数：doc 行优先，NULL（历史行）回落全局默认并回写（保证可复现）
            eff_size = doc.get("chunk_size") or self.settings.kb_chunk_size
            eff_overlap = doc.get("chunk_overlap") or self.settings.kb_chunk_overlap
            if doc.get("chunk_size") is None:
                await dbmod.update_kb_document_parse_params(
                    self.pool, doc_id, eff_size, eff_overlap
                )

            chunks = self._build_chunks(
                raw, doc["mime"], str(kb_id), str(doc_id), doc["filename"],
                chunk_size=eff_size, chunk_overlap=eff_overlap,
            )
            if not chunks:
                raise ValueError("解析后无可索引文本（空文档或解析失败）")

            store = self.vector_factory.get(str(kb_id), dims)
            # 幂等：先删该文档旧 chunk（应对重传/重试/重建；新块数少于旧块时清残留）
            self.vector_factory.delete_by_doc_id(str(kb_id), str(doc_id))

            # 分批嵌入 + 入库：按 embedding_batch_size 切片，逐批 add_documents（各自嵌入+写ES），
            # 每批后回写 progress。单批耗时随硬件而定（NAS CPU ~2s/文本）⇒ progress 粒度 = 单批。
            total = len(chunks)
            batch_size = max(1, self.settings.embedding_batch_size)
            for start in range(0, total, batch_size):
                batch = chunks[start:start + batch_size]
                batch_ids = [f"{doc_id}_{i}" for i in range(start, start + len(batch))]
                store.add_documents(batch, ids=batch_ids)
                await dbmod.update_kb_document_status(
                    self.pool, doc_id, status="indexing",
                    progress=_embed_progress(start + len(batch), total),
                )

            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="indexed", chunk_count=total,
                page_count=doc.get("page_count"), progress=100,
            )
            logger.info(
                "ingested doc=%s chunks=%d kb=%s dims=%s chunk=%d/%d",
                doc_id, total, kb_id, dims, eff_size, eff_overlap,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("ingest failed doc=%s kb=%s", doc_id, kb_id)
            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="failed", error=str(e)[:500]
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
        for d, score in merged:
            md = d.metadata or {}
            results.append(
                {
                    "content": d.page_content,
                    "score": float(score),
                    "filename": md.get("filename", ""),
                    "doc_id": md.get("doc_id"),
                    "page": md.get("page"),
                    "chunk_index": md.get("chunk_index"),
                    "kb_id": md.get("kb_id"),
                }
            )
        return results

    # ============================ Parsing helpers ===========================

    def _build_chunks(
        self,
        raw: bytes,
        mime: str,
        kb_id: str,
        doc_id: str,
        filename: str,
        chunk_size: int,
        chunk_overlap: int,
    ) -> list[Document]:
        """解析（pdf 逐页 / docx 逐段）→ 分块（按文档生效参数构造 splitter）→ 带 metadata。

        RecursiveCharacterTextSplitter 无状态且构造廉价，按参即时构造不缓存。
        """
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        pages = self._extract_pages(raw, mime)
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
        """→ [(page_no, text)]。pdf 逐页；docx 整篇作单页（无原生分页）。"""
        if is_pdf(mime):
            try:
                import fitz  # pymupdf

                out: list[tuple[int, str]] = []
                with fitz.open(stream=raw, filetype="pdf") as pdf:
                    for i in range(pdf.page_count):
                        out.append((i + 1, pdf[i].get_text("text")))
                return out
            except Exception:  # noqa: BLE001
                logger.warning("pdf 解析失败，将作空文档处理")
                return []
        if is_docx(mime):
            try:
                from docx import Document

                doc = Document(io.BytesIO(raw))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                return [(1, text)] if text else []
            except Exception:  # noqa: BLE001
                logger.warning("docx 解析失败，将作空文档处理")
                return []
        return []

    async def _read_bytes_async(self, bucket_key: str) -> bytes:
        """MinIO 取字节（同步客户端，丢线程池避免阻塞事件循环）。"""
        import asyncio

        return await asyncio.to_thread(self.minio.get_object, bucket_key)
