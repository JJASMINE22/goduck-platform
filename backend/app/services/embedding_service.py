"""向量模型注册管理（镜像 ModelService）：显式预检 + 类型化异常。

- 注册/语义变更时端点实证：embed 一条探针拿真实维度，声明 dims ≠ 实测
  即拒绝（消灭手填错维度）；连通性同轮验证。
- 语义字段（端点/型号/维度/凭据）被知识库引用时不可变：静默变更会让既有
  索引的向量空间失配（维度对、语义错的静默错配）。name/batch_size/
  timeout/enabled 自由编辑。
"""

from __future__ import annotations

import uuid
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.services.embedding import build_from_row


class EmbeddingReferencedError(Exception):
    """语义变更/删除被引用阻断：携带引用方知识库，提示先换绑（不级联）。"""

    def __init__(self, referencing: list[dict]):
        self.referencing = referencing
        names = ", ".join(k["name"] for k in referencing)
        super().__init__(f"{len(referencing)} 个知识库绑定该向量模型: {names}")


async def _probe(row: dict) -> int:
    """端点实证：embed 一条探针 → 真实维度（连通 + 维度双重校验）。"""
    try:
        vec = await build_from_row(row).aembed_query("维度探测")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"向量端点探测失败（{row['base_url']}）: {e}") from e
    if not vec:
        raise ValueError("向量端点返回空向量")
    return len(vec)


def _assert_dims(dims: Optional[int], probed: int) -> None:
    if dims is not None and dims != probed:
        raise ValueError(f"声明维度 {dims} 与端点实测 {probed} 不一致，请修正或留空")


class EmbeddingService:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def create(
        self,
        *,
        name: str,
        provider_kind: str,
        model_type: str,
        api_key: str,
        base_url: str,
        dims: Optional[int],
        batch_size: int,
        timeout: int,
        check_ctx_length: bool,
        enabled: bool,
        creator: str,
    ) -> dict:
        _assert_dims(dims, await _probe({
            "provider_kind": provider_kind, "model_type": model_type, "api_key": api_key,
            "base_url": base_url, "dims": dims, "batch_size": batch_size,
            "timeout": timeout, "check_ctx_length": check_ctx_length,
        }))
        if await dbmod.get_embedding_by_name(self.pool, name) is not None:
            raise FileExistsError(name)  # router maps -> 409
        return await dbmod.insert_embedding(
            self.pool,
            name=name, provider_kind=provider_kind, model_type=model_type,
            api_key=api_key, base_url=base_url, dims=dims,
            batch_size=batch_size, timeout=timeout,
            check_ctx_length=check_ctx_length, enabled=enabled, creator=creator,
        )

    async def update(
        self,
        embedding_id: uuid.UUID,
        *,
        name: str,
        provider_kind: str,
        model_type: str,
        api_key: Optional[str],
        base_url: str,
        dims: Optional[int],
        batch_size: int,
        timeout: int,
        check_ctx_length: bool,
        enabled: bool,
    ) -> dict:
        existing = await dbmod.get_embedding(self.pool, embedding_id)
        if existing is None:
            raise KeyError(f"embedding {embedding_id} not found")  # router maps -> 404
        if name != existing["name"]:
            if await dbmod.get_embedding_by_name(self.pool, name) is not None:
                raise FileExistsError(name)  # router maps -> 409

        effective_key = api_key if api_key is not None else existing["api_key"]
        semantic_changed = (
            provider_kind != existing["provider_kind"]
            or model_type != existing["model_type"]
            or effective_key != existing["api_key"]
            or base_url != existing["base_url"]
            or dims != existing["dims"]
            or check_ctx_length != existing["check_ctx_length"]
        )
        if semantic_changed:
            referencing = await dbmod.list_kbs_by_embedding(self.pool, embedding_id)
            if referencing:
                raise EmbeddingReferencedError(referencing)  # router maps -> 409
            _assert_dims(dims, await _probe({
                "provider_kind": provider_kind, "model_type": model_type,
                "api_key": effective_key, "base_url": base_url, "dims": dims,
                "batch_size": batch_size, "timeout": timeout,
                "check_ctx_length": check_ctx_length,
            }))
        return await dbmod.update_embedding(
            self.pool,
            embedding_id,
            name=name, provider_kind=provider_kind, model_type=model_type,
            api_key=api_key, base_url=base_url, dims=dims,
            batch_size=batch_size, timeout=timeout,
            check_ctx_length=check_ctx_length, enabled=enabled,
        )

    async def delete(self, embedding_id: uuid.UUID) -> None:
        """Delete an embedding. Block (referenced) if KBs still bind it."""
        if await dbmod.get_embedding_public(self.pool, embedding_id) is None:
            raise KeyError(f"embedding {embedding_id} not found")  # router maps -> 404
        referencing = await dbmod.list_kbs_by_embedding(self.pool, embedding_id)
        if referencing:
            raise EmbeddingReferencedError(referencing)  # router maps -> 409
        await dbmod.delete_embedding(self.pool, embedding_id)

    async def list_embeddings(self) -> list[dict]:
        """全部注册行（api_key 永不返回）。"""
        return await dbmod.list_embeddings(self.pool)
