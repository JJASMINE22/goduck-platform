"""Dependency injection: db pool / model / checkpointer / store / mcp / registry.

Replaces the old `base_confs` module-level global singletons. All shared state
lives on `app.state` and is exposed via FastAPI `Depends`.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User identity (migrated from scripts/pg_compiled_subagent_test_run.SimpleUser)
# ---------------------------------------------------------------------------
class SimpleUser:
    """Minimal BaseUser-protocol implementation for store namespace isolation."""

    def __init__(self, identity: str):
        self._identity = identity

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self._identity

    @property
    def permissions(self) -> list:
        return []

    def __getitem__(self, key):
        return getattr(self, key)

    def __contains__(self, key):
        return hasattr(self, key)

    def __iter__(self):
        return iter(["identity", "is_authenticated", "display_name", "permissions"])


# ---------------------------------------------------------------------------
# Core infra singletons — built once at startup, held on app.state
# ---------------------------------------------------------------------------
class AppState:
    """Holds long-lived infra objects attached to FastAPI app.state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: Optional[AsyncConnectionPool] = None
        self.checkpointer: Optional[AsyncPostgresSaver] = None
        self.store: Optional[AsyncPostgresStore] = None
        # services (wired in main lifespan)
        self.mcp_lifecycle = None
        self.agent_registry = None
        self.stream_adapter = None
        self.minio_storage = None
        self.model_store = None
        self.content_composer = None

    async def startup(self) -> None:
        s = self.settings

        # Postgres pool — shared by business tables + checkpointer + store
        self.pool = AsyncConnectionPool(
            conninfo=s.database_url,
            max_size=s.db_pool_max_size,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        await self.pool.open()

        # LangGraph checkpointer (native tables)
        self.checkpointer = AsyncPostgresSaver(conn=self.pool)
        await self.checkpointer.setup()

        # LangGraph store (per-user memory namespace)
        self.store = AsyncPostgresStore(conn=self.pool)
        await self.store.setup()

        # Business tables (agent / agent_config_history / session / mcp_server)
        from app.models import db as dbmod
        await dbmod.init_business_schema(self.pool)

        logger.info("AppState startup complete")

    async def shutdown(self) -> None:
        # store/checkpointer share the pool; closing the pool releases connections.
        # AsyncPostgresStore has no close() — nothing to release explicitly.
        if self.pool is not None:
            await self.pool.close()
        logger.info("AppState shutdown complete")


async def init_state(app: FastAPI) -> AppState:
    """Build AppState, run startup, attach to app.state, return it."""
    st = AppState(get_settings())
    await st.startup()
    app.state.appstate = st
    return st


# ---------------------------------------------------------------------------
# FastAPI dependency accessors
# ---------------------------------------------------------------------------
def get_appstate(request: Request) -> AppState:
    return request.app.state.appstate


def get_pool(request: Request) -> AsyncConnectionPool:
    return get_appstate(request).pool


def get_checkpointer(request: Request) -> AsyncPostgresSaver:
    return get_appstate(request).checkpointer


def get_store(request: Request) -> AsyncPostgresStore:
    return get_appstate(request).store


def get_settings_dep(request: Request) -> Settings:
    return get_appstate(request).settings
