"""Registered model storage + validation (mirrors AgentService).

Explicit pre-checks raise typed exceptions (mapped to HTTP codes by the router);
no reliance on DB-exception fallback. Keep model logic symmetric with agent ops.
"""
from __future__ import annotations

import uuid
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.services.model_store import build_chat_model


class ModelInUseError(Exception):
    """Raised when deleting a model still referenced by agents.

    Carries the referencing agents so the caller can surface them to the user
    for manual rebinding (we never cascade-delete agents).
    """

    def __init__(self, referencing: list[dict]):
        self.referencing = referencing
        names = ", ".join(a["name"] for a in referencing)
        super().__init__(f"{len(referencing)} 个智能体仍在使用该模型: {names}")


def _ensure_buildable(provider_kind: str, model_type: str, api_key: str, base_url: Optional[str]) -> None:
    """Construction-level pre-check (no network). Guarantees persisted ⟹ instantiable.

    Catches param-class errors only (bad provider_kind, missing model_type, ...).
    Does NOT verify connectivity — that is the opt-in /models/test probe. Raises
    ValueError (router maps -> 400) so misconfigured models are rejected at write
    time rather than silently dropped by ModelStore.refresh().
    """
    try:
        build_chat_model(provider_kind, model_type, api_key, base_url)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"模型参数无法实例化: {e}") from e


class ModelService:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def create(
        self,
        name: str,
        provider_kind: str,
        model_type: str,
        api_key: str,
        base_url: Optional[str],
        supports_vision: bool,
        enabled: bool,
    ) -> dict:
        _ensure_buildable(provider_kind, model_type, api_key, base_url)
        if await dbmod.get_model_by_name(self.pool, name) is not None:
            raise FileExistsError(name)  # router maps -> 409
        return await dbmod.insert_model(
            self.pool,
            name=name,
            provider_kind=provider_kind,
            model_type=model_type,
            api_key=api_key,
            base_url=base_url,
            supports_vision=supports_vision,
            enabled=enabled,
        )

    async def update(
        self,
        model_id: uuid.UUID,
        *,
        name: str,
        provider_kind: str,
        model_type: str,
        api_key: Optional[str],
        base_url: Optional[str],
        supports_vision: bool,
        enabled: bool,
    ) -> dict:
        existing = await dbmod.get_model_public(self.pool, model_id)
        if existing is None:
            raise KeyError(f"model {model_id} not found")  # router maps -> 404
        # key rotation path: re-validate constructability with the new key
        if api_key is not None:
            _ensure_buildable(provider_kind, model_type, api_key, base_url)
        # name collision against a DIFFERENT model
        if name != existing["name"]:
            clash = await dbmod.get_model_by_name(self.pool, name)
            if clash is not None:
                raise FileExistsError(name)  # router maps -> 409
        return await dbmod.update_model(
            self.pool,
            model_id,
            name=name,
            provider_kind=provider_kind,
            model_type=model_type,
            api_key=api_key,
            base_url=base_url,
            supports_vision=supports_vision,
            enabled=enabled,
        )

    async def delete(self, model_id: uuid.UUID) -> None:
        """Delete a model. Block (ModelInUseError) if agents still reference it."""
        if await dbmod.get_model_public(self.pool, model_id) is None:
            raise KeyError(f"model {model_id} not found")  # router maps -> 404
        referencing = await dbmod.list_agents_by_model(self.pool, model_id)
        if referencing:
            raise ModelInUseError(referencing)  # router maps -> 409
        await dbmod.delete_model(self.pool, model_id)

    async def list_models(self) -> list[dict]:
        return await dbmod.list_models(self.pool)
