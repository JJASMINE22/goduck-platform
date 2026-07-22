"""psycopg3-direct persistence layer.

Shares the SAME `AsyncConnectionPool` as the LangGraph checkpointer / store.
Business tables: agent / agent_config_history / session / mcp_server.
LangGraph-native tables (checkpoints / checkpoint_writes / checkpoint_blobs / store)
are created by AsyncPostgresSaver/Store `.setup()`.

Design (plan §1, 方案 B):
- agent.id 全程稳定不变; 更新 = in-place 覆写 config_json + 追加 agent_config_history 快照。
- 更新即级联删该 agent 的全部会话, 并清理 LangGraph checkpoints 残留 (两类历史区分:
  会话历史删, 配置历史保留)。

Notes:
- psycopg3 uses `%s` placeholders (NOT asyncpg's `$1`).
- Native JSON/JSONB adaptation — pass python dict directly, no json.dumps.
- All multi-step ops run inside explicit transactions.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

# LangGraph AsyncPostgresSaver default table names (cleaned on agent update).
_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")

# ---------------------------------------------------------------------------
# DDL — business tables only (idempotent)
# ---------------------------------------------------------------------------
BUSINESS_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS model (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR NOT NULL UNIQUE,
    provider_kind   VARCHAR NOT NULL DEFAULT 'openai_compatible',
    model_type      TEXT NOT NULL,
    api_key         TEXT NOT NULL,
    base_url        TEXT,
    supports_vision BOOLEAN NOT NULL DEFAULT false,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR NOT NULL UNIQUE,
    description  TEXT,
    config_json  JSONB   NOT NULL,
    model_id     UUID,
    status       VARCHAR NOT NULL DEFAULT 'active',
    creator      VARCHAR NOT NULL,
    updater      VARCHAR,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_config_history (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
    config_json  JSONB   NOT NULL,
    model_id     UUID,
    action       VARCHAR NOT NULL,
    operator     VARCHAR NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ach_agent ON agent_config_history(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
    user_id      VARCHAR NOT NULL,
    thread_id    VARCHAR NOT NULL UNIQUE,
    title        TEXT,
    status       VARCHAR NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_session_agent ON session(agent_id);
CREATE INDEX IF NOT EXISTS idx_session_user  ON session(user_id);

CREATE TABLE IF NOT EXISTS mcp_server (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR NOT NULL UNIQUE,
    kind         VARCHAR NOT NULL,
    config_json  JSONB   NOT NULL,
    enabled      BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    user_id       VARCHAR NOT NULL,
    bucket_key    TEXT NOT NULL,
    filename      TEXT NOT NULL,
    mime          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    size          BIGINT NOT NULL,
    page_count    INT,
    preview       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_upload_session ON upload(session_id);
CREATE INDEX IF NOT EXISTS idx_upload_user ON upload(user_id);
"""

# Idempotent column migrations for pre-existing tables (CREATE TABLE IF NOT
# EXISTS won't add columns to already-present tables). Each is guarded so it's
# safe to run repeatedly.
_MIGRATIONS = [
    "ALTER TABLE agent ADD COLUMN IF NOT EXISTS model_id UUID",
    "ALTER TABLE agent_config_history ADD COLUMN IF NOT EXISTS model_id UUID",
]


async def init_business_schema(pool: AsyncConnectionPool) -> None:
    """Create business tables if absent + run column migrations. Safe to repeat."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(BUSINESS_DDL)
            for stmt in _MIGRATIONS:
                await cur.execute(stmt)
    logger.info("business schema ready (model / agent / agent_config_history / session / mcp_server / upload)")


# ============================ Agent ops ===================================

async def create_agent(
    pool: AsyncConnectionPool,
    name: str,
    description: Optional[str],
    config_json: dict,
    model_id: uuid.UUID,
    creator: str,
) -> dict:
    """Atomically create agent + first history snapshot (action=create)."""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "INSERT INTO agent (name, description, config_json, model_id, creator) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING id, name, description, config_json, model_id, status, "
                    "creator, updater, created_at, updated_at",
                    (name, description, Json(config_json), model_id, creator),
                )
                agent = await cur.fetchone()
                await cur.execute(
                    "INSERT INTO agent_config_history (agent_id, config_json, model_id, action, operator) "
                    "VALUES (%s, %s, %s, 'create', %s)",
                    (agent["id"], Json(config_json), model_id, creator),
                )
    return agent


async def update_agent(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    config_json: dict,
    model_id: uuid.UUID,
    description: Optional[str],
    updater: str,
) -> Optional[dict]:
    """Update agent config + model_id in-place + append history snapshot.

    Side effects (same tx): cascade-delete this agent's sessions + clean LangGraph
    checkpoints for those thread_ids. Returns None if agent not found.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # collect soon-to-be-orphaned thread_ids BEFORE deleting sessions
                await cur.execute(
                    "SELECT thread_id FROM session WHERE agent_id = %s",
                    (agent_id,),
                )
                thread_rows = await cur.fetchall()
                thread_ids = [r["thread_id"] for r in thread_rows]

                await cur.execute(
                    "UPDATE agent SET config_json = %s, model_id = %s, "
                    "description = COALESCE(%s, description), "
                    "updater = %s, updated_at = now() WHERE id = %s "
                    "RETURNING id, name, description, config_json, model_id, status, "
                    "creator, updater, created_at, updated_at",
                    (Json(config_json), model_id, description, updater, agent_id),
                )
                agent = await cur.fetchone()
                if agent is None:
                    return None

                await cur.execute(
                    "INSERT INTO agent_config_history (agent_id, config_json, model_id, action, operator) "
                    "VALUES (%s, %s, %s, 'update', %s)",
                    (agent_id, Json(config_json), model_id, updater),
                )

                # delete this agent's sessions (CASCADE-friendly explicit delete)
                await cur.execute("DELETE FROM session WHERE agent_id = %s", (agent_id,))

                # clean LangGraph checkpoint tables for orphaned thread_ids
                if thread_ids:
                    for tbl in _CHECKPOINT_TABLES:
                        await cur.execute(
                            f"DELETE FROM {tbl} WHERE thread_id = ANY(%s)",
                            (thread_ids,),
                        )
    if thread_ids:
        logger.info("agent %s updated; purged %d sessions + checkpoint state", agent_id, len(thread_ids))
    return agent


async def get_history_snapshot(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    history_id: uuid.UUID,
) -> Optional[dict]:
    """Return {config_json, model_id} for a history snapshot, or None if absent."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT config_json, model_id FROM agent_config_history "
                "WHERE id = %s AND agent_id = %s",
                (history_id, agent_id),
            )
            return await cur.fetchone()


async def restore_agent(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    history_id: uuid.UUID,
    operator: str,
) -> Optional[dict]:
    """Restore an agent's config + model_id from a history snapshot (acts as update).

    The caller MUST validate the snapshot's model_id beforehand (see
    get_history_snapshot): history.model_id has no FK, so legacy snapshots may
    be NULL or point to a deleted model; restoring either would leave the agent
    unbuildable — and update_agent purge sessions+checkpoints before failing.
    """
    snapshot = await get_history_snapshot(pool, agent_id, history_id)
    if snapshot is None:
        return None
    return await update_agent(
        pool, agent_id, snapshot["config_json"], snapshot["model_id"],
        description=None, updater=operator,
    )


async def list_agents(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, description, model_id, status, creator, updater, created_at, updated_at "
                "FROM agent ORDER BY updated_at DESC"
            )
            rows = await cur.fetchall()
    return rows


async def get_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, description, config_json, model_id, status, creator, updater, "
                "created_at, updated_at FROM agent WHERE id = %s",
                (agent_id,),
            )
            row = await cur.fetchone()
    return row


async def get_agent_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, description, config_json, model_id, status, creator, updater, "
                "created_at, updated_at FROM agent WHERE name = %s",
                (name,),
            )
            row = await cur.fetchone()
    return row


async def list_active_agents(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, config_json FROM agent WHERE status = 'active'"
            )
            rows = await cur.fetchall()
    return rows


async def archive_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE agent SET status = 'archived', updated_at = now() WHERE id = %s "
                "RETURNING id, name, description, config_json, model_id, status, creator, updater, "
                "created_at, updated_at",
                (agent_id,),
            )
            row = await cur.fetchone()
    return row


async def list_history(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, config_json, model_id, action, operator, created_at "
                "FROM agent_config_history WHERE agent_id = %s ORDER BY created_at DESC",
                (agent_id,),
            )
            rows = await cur.fetchall()
    return rows


# ============================ Session ops =================================

async def create_session(
    pool: AsyncConnectionPool,
    agent_id: uuid.UUID,
    user_id: str,
    thread_id: str,
    title: Optional[str],
) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO session (agent_id, user_id, thread_id, title) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING id, agent_id, user_id, thread_id, title, status, created_at, updated_at",
                (agent_id, user_id, thread_id, title),
            )
            row = await cur.fetchone()
    return row


async def list_sessions_by_user(pool: AsyncConnectionPool, user_id: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT s.id, s.agent_id, s.user_id, s.thread_id, s.title, s.status, "
                "s.created_at, s.updated_at, a.name AS agent_name "
                "FROM session s JOIN agent a ON a.id = s.agent_id "
                "WHERE s.user_id = %s ORDER BY s.updated_at DESC",
                (user_id,),
            )
            rows = await cur.fetchall()
    return rows


async def get_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, agent_id, user_id, thread_id, title, status, created_at, updated_at "
                "FROM session WHERE id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
    return row


async def archive_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE session SET status = 'archived', updated_at = now() WHERE id = %s "
                "RETURNING id, agent_id, user_id, thread_id, title, status, created_at, updated_at",
                (session_id,),
            )
            row = await cur.fetchone()
    return row


async def touch_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE session SET updated_at = now() WHERE id = %s", (session_id,))


# ============================ MCP server ops ==============================

async def list_mcp_servers(pool: AsyncConnectionPool) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server ORDER BY created_at"
            )
            rows = await cur.fetchall()
    return rows


async def upsert_mcp_server(
    pool: AsyncConnectionPool,
    name: str,
    kind: str,
    config_json: dict,
    enabled: bool,
) -> dict:
    """Insert or update (by unique name) an MCP server registration."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO mcp_server (name, kind, config_json, enabled) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET "
                "kind = EXCLUDED.kind, config_json = EXCLUDED.config_json, enabled = EXCLUDED.enabled "
                "RETURNING id, name, kind, config_json, enabled, created_at",
                (name, kind, Json(config_json), enabled),
            )
            row = await cur.fetchone()
    return row


async def delete_mcp_server(pool: AsyncConnectionPool, server_id: uuid.UUID) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM mcp_server WHERE id = %s", (server_id,))
            return cur.rowcount > 0


# ============================ Upload ops ==================================

async def insert_upload(
    pool: AsyncConnectionPool,
    session_id: uuid.UUID,
    user_id: str,
    bucket_key: str,
    filename: str,
    mime: str,
    kind: str,
    size: int,
    page_count: Optional[int],
    preview: Optional[str],
) -> dict:
    """Insert an upload metadata row. kind/mime resolved server-side at upload."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO upload (session_id, user_id, bucket_key, filename, mime, kind, "
                "size, page_count, preview) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, session_id, user_id, bucket_key, filename, mime, kind, size, "
                "page_count, preview, created_at",
                (session_id, user_id, bucket_key, filename, mime, kind, size, page_count, preview),
            )
            row = await cur.fetchone()
    return row


async def get_upload(pool: AsyncConnectionPool, upload_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, session_id, user_id, bucket_key, filename, mime, kind, size, "
                "page_count, preview, created_at FROM upload WHERE id = %s",
                (upload_id,),
            )
            row = await cur.fetchone()
    return row


async def list_uploads_by_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, session_id, user_id, bucket_key, filename, mime, kind, size, "
                "page_count, preview, created_at FROM upload "
                "WHERE session_id = %s ORDER BY created_at DESC",
                (session_id,),
            )
            rows = await cur.fetchall()
    return rows


# ============================ Model ops ==================================

_MODEL_COLUMNS = (
    "id, name, provider_kind, model_type, base_url, supports_vision, enabled, "
    "created_at, updated_at"
)


async def insert_model(
    pool: AsyncConnectionPool,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: Optional[str],
    supports_vision: bool,
    enabled: bool,
) -> dict:
    """Insert a registered model definition. api_key stored at the row level."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"INSERT INTO model (name, provider_kind, model_type, api_key, base_url, "
                f"supports_vision, enabled) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_MODEL_COLUMNS}",
                (name, provider_kind, model_type, api_key, base_url, supports_vision, enabled),
            )
            row = await cur.fetchone()
    return row


async def update_model(
    pool: AsyncConnectionPool,
    model_id: uuid.UUID,
    *,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: Optional[str],
    base_url: Optional[str],
    supports_vision: bool,
    enabled: bool,
) -> Optional[dict]:
    """Update a registered model's mutable fields. model_id (PK) never changes.

    api_key=None means "keep existing"; otherwise overwrite (key rotation).
    """
    set_api_key = "api_key = %s" if api_key is not None else "api_key = model.api_key"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = (
                [name, provider_kind, model_type, base_url, supports_vision, enabled]
                + ([api_key] if api_key is not None else [])
                + [model_id]
            )
            await cur.execute(
                f"UPDATE model SET name = %s, provider_kind = %s, model_type = %s, "
                f"base_url = %s, supports_vision = %s, enabled = %s, {set_api_key}, "
                f"updated_at = now() WHERE id = %s RETURNING {_MODEL_COLUMNS}",
                tuple(params),
            )
            row = await cur.fetchone()
    return row


async def get_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> Optional[dict]:
    """Full row incl. api_key (server-side only)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS}, api_key FROM model WHERE id = %s",
                (model_id,),
            )
            row = await cur.fetchone()
    return row


async def get_model_public(pool: AsyncConnectionPool, model_id: uuid.UUID) -> Optional[dict]:
    """Row WITHOUT api_key (for API responses)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model WHERE id = %s",
                (model_id,),
            )
            row = await cur.fetchone()
    return row


async def list_models(pool: AsyncConnectionPool) -> list[dict]:
    """List WITHOUT api_key (for API responses)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model ORDER BY created_at"
            )
            rows = await cur.fetchall()
    return rows


async def list_enabled_models(pool: AsyncConnectionPool) -> list[dict]:
    """Full rows incl. api_key for ModelStore startup loading."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS}, api_key FROM model WHERE enabled = true"
            )
            rows = await cur.fetchall()
    return rows


async def delete_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM model WHERE id = %s", (model_id,))
            return cur.rowcount > 0


async def get_model_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    """Public row (no api_key) by unique name — for explicit create/update conflict checks."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_MODEL_COLUMNS} FROM model WHERE name = %s",
                (name,),
            )
            row = await cur.fetchone()
    return row


async def list_agents_by_model(pool: AsyncConnectionPool, model_id: uuid.UUID) -> list[dict]:
    """Agents referencing a model (id + name) — for delete-referenced blocking."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name FROM agent WHERE model_id = %s ORDER BY name",
                (model_id,),
            )
            rows = await cur.fetchall()
    return rows
