"""后台入库池（plan §5.4）：限并发 fire-and-forget，仿 StreamTracker 思路。

- submit(kb_id, doc_id, raw_bytes=None)：投递一个入库任务，立即返回（不阻塞请求）。
- 并发由 Semaphore(kb_ingestion_concurrency) 限制，避免大文档批量 embed 压垮 TEI/ES。
- 异常吞掉：ingest_document 内部已把失败写回 PG 状态卡（failed），此处仅记日志。
- shutdown：等待在途任务完成（app 关闭时优雅收尾）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from app.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


class IngestPool:
    def __init__(self, knowledge_service: KnowledgeService, concurrency: int):
        self.svc = knowledge_service
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._tasks: set[asyncio.Task] = set()

    def submit(
        self,
        kb_id: uuid.UUID,
        doc_id: uuid.UUID,
        raw_bytes: Optional[bytes] = None,
    ) -> None:
        """投递入库任务（fire-and-forget）。raw_bytes 仅 KB_STORE_MINIO=false 时透传。"""
        task = asyncio.create_task(self._run(kb_id, doc_id, raw_bytes))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self, kb_id: uuid.UUID, doc_id: uuid.UUID, raw_bytes: Optional[bytes]
    ) -> None:
        async with self._sem:
            try:
                await self.svc.ingest_document(kb_id, doc_id, raw_bytes=raw_bytes)
            except Exception:  # noqa: BLE001
                # ingest_document 已在内部把失败写回状态卡；此处兜底吞异常，避免任务泄漏。
                logger.exception("ingest task crashed doc=%s kb=%s", doc_id, kb_id)

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def shutdown(self) -> None:
        """等待在途任务完成（关闭收尾）。"""
        if not self._tasks:
            return
        logger.info("ingest pool shutting down: %d task(s) pending", len(self._tasks))
        await asyncio.gather(*self._tasks, return_exceptions=True)
