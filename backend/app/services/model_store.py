"""Registered-model store + ChatOpenAI factory (mirrors MCPLifecycle).

Loads enabled model definitions from the `model` table at startup, builds and
caches one chat client per model_id, and resolves capability (supports_vision).

Design:
- provider_kind is a "client family" enum, not a vendor name. Today everything
  routes through the single OpenAI-compatible family (ChatOpenAI + base_url).
- The build factory is the single dispatch seam; adding a native-SDK family
  later means one new branch + pip install, no schema change.
- Cache keyed by model_id so key rotation (update) re-uses the build pipeline;
  refresh() rebuilds on demand.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod

logger = logging.getLogger(__name__)


def build_chat_model(
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: Optional[str],
) -> BaseChatModel:
    """Construct a chat client by provider family. Single dispatch seam.

    Adding a non-OpenAI-compatible family later = one new branch here + the
    matching langchain-<vendor> package. Schema stays unchanged.
    """
    if provider_kind == "openai_compatible":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_type,
            api_key=api_key,
            base_url=base_url or None,
        )
    raise ValueError(f"不支持的 provider_kind: {provider_kind}")


class ModelStore:
    """Manage registered model definitions + cached chat clients (DB-driven)."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool
        # model_id (uuid) -> {client, supports_vision, model_type}
        self._models: dict[uuid.UUID, dict] = {}
        self._started = False

    async def startup(self) -> None:
        await self.refresh()
        self._started = True
        logger.info("ModelStore started: %d enabled models", len(self._models))

    async def refresh(self) -> None:
        """Reload enabled models from DB + (re)build clients (fault-isolated)."""
        rows = await dbmod.list_enabled_models(self.pool)
        new_models: dict[uuid.UUID, dict] = {}
        for row in rows:
            mid = row["id"]
            try:
                client = build_chat_model(
                    row["provider_kind"],
                    row["model_type"],
                    row["api_key"],
                    row.get("base_url"),
                )
                new_models[mid] = {
                    "client": client,
                    "supports_vision": row["supports_vision"],
                    "model_type": row["model_type"],
                    "name": row["name"],
                }
            except Exception as e:  # noqa: BLE001
                logger.warning("model '%s' (%s) build failed (skipped): %s", row.get("name"), mid, e)
        self._models = new_models

    def get(self, model_id: uuid.UUID) -> BaseChatModel:
        """Return the cached chat client for a model_id. Raise if unavailable."""
        entry = self._models.get(model_id)
        if entry is None:
            raise KeyError(
                f"model {model_id} 不可用（未注册 / 已禁用 / 构建失败），请先在 /models 注册并启用"
            )
        return entry["client"]

    def supports_vision(self, model_id: uuid.UUID) -> bool:
        entry = self._models.get(model_id)
        return bool(entry and entry["supports_vision"])

    def has(self, model_id: uuid.UUID) -> bool:
        return model_id in self._models

    def model_type_of(self, model_id: uuid.UUID) -> Optional[str]:
        entry = self._models.get(model_id)
        return entry["model_type"] if entry else None

    async def shutdown(self) -> None:
        self._models.clear()
        self._started = False
        logger.info("ModelStore shut down")
