"""向量模型注册表（只读 PG）—— kb_id → embedding 注册行解析。

与 backend 的差异：本侧无写入、无缓存，逐次解析（每次 kb_search 一条
查询，量级 = 用户所选库数）；连接池常驻 lifespan。

故障语义（fail-open）：DATABASE_URL 为空 = 注册表关闭；PG 不可达 / 查询
失败 → 告警 + 返回空 dict → 调用方整体回落 env legacy 路径（行为=历史
版本）。回落是热切换期的回滚保险，不阻断检索主链路。

行含 api_key：仅进程内构建嵌入客户端使用，绝不进日志 / 工具返回值。
"""

from __future__ import annotations

import logging
from typing import Optional

from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_RESOLVE_SQL = (
    "SELECT kb.id::text AS kb_id, e.id::text, e.name, e.model_type, e.api_key, "
    "e.base_url, e.dims, e.batch_size, e.timeout, e.check_ctx_length "
    "FROM knowledge_base kb JOIN embedding e ON e.id = kb.embedding_id "
    "WHERE e.enabled AND kb.id::text = ANY(%s)"
)


class EmbeddingRegistry:
    """kb_ids → {kb_id: embedding 行}（enabled 行；未绑定/悬空不在返回值中）。"""

    def __init__(self, database_url: str):
        self._url = database_url
        self._pool = None

    async def startup(self) -> None:
        if not self._url:
            logger.info("embedding 注册表关闭（DATABASE_URL 未配置，检索走 env 路径）")
            return
        from psycopg_pool import AsyncConnectionPool

        try:
            self._pool = AsyncConnectionPool(
                conninfo=self._url, min_size=0, max_size=2, open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0},
            )
            await self._pool.open()
            logger.info("embedding 注册表就绪（只读 PG）")
        except Exception as e:  # noqa: BLE001
            logger.warning("embedding 注册表连接失败（回落 env 路径）: %s", e)
            self._pool = None

    async def resolve(self, kb_ids: list[str]) -> dict[str, dict]:
        if self._pool is None or not kb_ids:
            return {}
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(_RESOLVE_SQL, (kb_ids,))
                    rows = await cur.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("embedding 注册表解析失败（回落 env 路径）: %s", e)
            return {}
        return {r["kb_id"]: dict(r) for r in rows}

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()
            logger.info("embedding 注册表已关闭")
