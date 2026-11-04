"""向量模型注册表（DB 驱动，镜像 ModelStore）。

加载 enabled 行 + 按行构建并缓存嵌入客户端（by embedding_id）；
/embeddings 变更后由路由层调 refresh() 重建（免重启生效）。

缓存一致性：语义字段（端点/型号/维度）被 KB 引用时不可变（service 守卫），
仅 name/batch_size/timeout/enabled 可编辑——refresh() 整体换血即无悬挂客户端；
vector_factory 派生 store 缓存由其 invalidate_embedding 同步失效。
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.services.embedding import build_from_row

logger = logging.getLogger(__name__)


class EmbeddingStore:
    """enabled 注册行 + 嵌入客户端缓存（行与客户端同生命周期）。"""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool
        # embedding_id -> {**row, client}
        self._entries: dict[uuid.UUID, dict] = {}

    async def startup(self) -> None:
        await self.refresh()
        logger.info("EmbeddingStore started: %d enabled embedding(s)", len(self._entries))

    async def refresh(self) -> None:
        """Reload enabled rows + (re)build clients (fault-isolated per row)."""
        rows = await dbmod.list_enabled_embeddings(self.pool)
        entries: dict[uuid.UUID, dict] = {}
        for row in rows:
            try:
                client = build_from_row(row)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "embedding '%s' (%s) client build failed (skipped): %s",
                    row.get("name"), row["id"], e,
                )
                continue
            entries[row["id"]] = {**row, "client": client}
        self._entries = entries

    def get(self, embedding_id: uuid.UUID) -> dict:
        """Return the registered entry (row + client). Raise if unavailable."""
        entry = self._entries.get(embedding_id)
        if entry is None:
            raise KeyError(
                f"向量模型 {embedding_id} 不可用（未注册 / 已禁用 / 构建失败）"
            )
        return entry

    def get_or_none(self, embedding_id: Optional[uuid.UUID]) -> Optional[dict]:
        """宽松取行：悬空引用 / None → None（调用方回落 legacy 路径）。"""
        if embedding_id is None:
            return None
        return self._entries.get(embedding_id)

    def client(self, embedding_id: uuid.UUID):
        """Cached embedding client for an embedding_id."""
        return self.get(embedding_id)["client"]

    def has(self, embedding_id: uuid.UUID) -> bool:
        return embedding_id in self._entries

    async def shutdown(self) -> None:
        self._entries.clear()
        logger.info("EmbeddingStore shut down")
