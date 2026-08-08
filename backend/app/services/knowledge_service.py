"""知识库领域服务（plan §5.3）：入库 + 文档/库生命周期 + 检索。

职责：
- KB/文档 CRUD（PG 状态卡）+ 入库（parse→split→embed→ES）+ 检索（ES kNN）。
- chunk 文本/向量真值入 Elasticsearch；PG 仅状态/元数据；MinIO 可选留底原始字节。

一致性模型（回应「杜绝 PG 持久化但 MinIO/ES 报错悬挂」）：
- 注册文档（register_document）：MinIO 上传在 PG 写入之前 → MinIO 失败即中止
  （不插 PG 行，无悬挂）；MinIO 成功后若 PG 写入失败则回滚删除 MinIO 对象。
- 入库（ingest_document）：异步，幂等（先按 doc_id 删旧 chunk 再写）；任一步失败
  → PG 置 failed（行保留=状态卡，MinIO 留底=可 reindex）。
- 删除（delete_document/delete_kb）：ES→MinIO→PG 顺序，ES/MinIO 失败尽力而为
  （记日志），PG 为「该文档是否存在」的真相源。

显式预检抛类型化异常（FileExistsError→409 / KeyError→404），由路由层映射。
"""
from __future__ import annotations

import io
import logging
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
    """维度漂移：目标 KB 记录的 dims != 全局 EMBEDDING_DIMS → 拦截检索。"""


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
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.kb_chunk_size,
            chunk_overlap=settings.kb_chunk_overlap,
        )

    # ============================ KB CRUD ====================================

    async def create_kb(self, name: str, description: Optional[str], creator: str) -> dict:
        if await dbmod.get_kb_by_name(self.pool, name) is not None:
            raise FileExistsError(name)  # -> 409
        new_id = uuid.uuid4()
        return await dbmod.create_kb(
            self.pool,
            name=name,
            description=description,
            es_index_name=f"kb_{new_id}",
            dims=self.settings.embedding_dims,  # 记录全局 dim，仅漂移检测
            creator=creator,
            kb_id=new_id,
        )

    async def update_kb(
        self, kb_id: uuid.UUID, name: str, description: Optional[str]
    ) -> dict:
        existing = await dbmod.get_kb(self.pool, kb_id)
        if existing is None:
            raise KeyError(f"kb {kb_id} not found")  # -> 404
        # 改名时校验目标 name 未被其它 KB 占用（index_name 不可变，禁止改）
        if name != existing["name"]:
            clash = await dbmod.get_kb_by_name(self.pool, name)
            if clash is not None:
                raise FileExistsError(name)  # -> 409
        return await dbmod.update_kb(self.pool, kb_id, name, description)

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
        """注册文档（pending）：MinIO 在前、PG 在后（一致性，杜绝悬挂）。

        KB 仅收 pdf/docx；返回状态卡行。原始字节留底与否取决于 KB_STORE_MINIO：
        - true：存 MinIO（bucket_key=kb/<kb_id>/<doc_id>），后续 ingest 从 MinIO 取。
        - false：bucket_key=None，调用方须把 raw 透传给 ingest（内存路径）。

        同名拦截：同 (kb_id, filename) 已有文档则直接拒绝（FileExistsError -> 409），
        不执行 MinIO 上传（避免无效字节落对象存储）。更新内容需用户先删除再上传。
        并发竞态（check 与 insert 之间）由 DB 唯一索引兜住，仍归一为同名冲突。
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

        # ② PG 状态卡（pending）；失败则回滚清 MinIO，避免悬挂字节
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

    async def reindex_document(self, kb_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        """重建索引：从 MinIO 留底重新 ingest（ingest_document 自带删旧 chunk 幂等）。"""
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

        raw_bytes：KB_STORE_MINIO=false 时由调用方透传内存字节；否则从 MinIO 取。
        嵌入维度 = 全局 EMBEDDING_DIMS（由嵌入客户端携带 dimensions）。
        """
        await dbmod.update_kb_document_status(self.pool, doc_id, status="indexing")
        try:
            doc = await dbmod.get_kb_document(self.pool, doc_id)
            if doc is None:
                raise KeyError(f"doc {doc_id} vanished during ingest")

            raw = raw_bytes
            if raw is None:
                if not doc.get("bucket_key"):
                    raise ValueError("无原始字节可入库（既未透传内存，也无 MinIO 留底）")
                raw = await self._read_bytes_async(doc["bucket_key"])

            chunks = self._build_chunks(raw, doc["mime"], str(kb_id), str(doc_id), doc["filename"])
            if not chunks:
                raise ValueError("解析后无可索引文本（空文档或解析失败）")

            store = self.vector_factory.get(str(kb_id))
            # 幂等：先删该文档旧 chunk（应对重传/重试/重建；新块数少于旧块时清残留）
            self.vector_factory.delete_by_doc_id(str(kb_id), str(doc_id))
            ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
            store.add_documents(chunks, ids=ids)

            await dbmod.update_kb_document_status(
                self.pool, doc_id, status="indexed", chunk_count=len(chunks),
                page_count=doc.get("page_count"),
            )
            logger.info("ingested doc=%s chunks=%d kb=%s", doc_id, len(chunks), kb_id)
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

        - kb_ids=None → 全部 KB（index='kb_*'）；否则逗号拼接。
        - doc_ids 非空 → 叠加 metadata.doc_id.keyword terms 过滤。
        - 维度漂移检测：目标 KB 记录 dims != 全局 EMBEDDING_DIMS → 抛 DimensionMismatchError。
        """
        k = k or self.settings.kb_top_k

        # 漂移检测（仅对显式指定的 KB；通配查询无法预知各库 dims，由全局单一 dim 保证一致）
        if kb_ids:
            kbs = await dbmod.get_kbs_by_ids(self.pool, kb_ids)
            if not kbs:
                return []
            for kb in kbs:
                if (
                    self.settings.embedding_dims is not None
                    and kb.get("dims") is not None
                    and kb["dims"] != self.settings.embedding_dims
                ):
                    raise DimensionMismatchError(
                        f"知识库 '{kb['name']}' 维度 {kb['dims']} 与当前全局维度 "
                        f"{self.settings.embedding_dims} 不一致，请重建该库"
                    )
            index = ",".join(f"kb_{i}" for i in kb_ids)
        else:
            index = "kb_*"

        store = self.vector_factory.get_for_index(index)
        flt: list[dict] = []
        if doc_ids:
            flt.append({"terms": {"metadata.doc_id.keyword": [str(d) for d in doc_ids]}})

        hits = store.similarity_search_with_score(query, k=k, filter=flt or None)
        results: list[dict] = []
        for d, score in hits:
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
        self, raw: bytes, mime: str, kb_id: str, doc_id: str, filename: str
    ) -> list[Document]:
        """解析（pdf 逐页 / docx 逐段）→ 分块 → 带 chunk metadata。"""
        pages = self._extract_pages(raw, mime)
        chunks: list[Document] = []
        for page_no, text in pages:
            if not text.strip():
                continue
            for ci, piece in enumerate(self._splitter.split_text(text)):
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
