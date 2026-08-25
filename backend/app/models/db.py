"""psycopg3-direct persistence layer.

Shares the SAME `AsyncConnectionPool` as the LangGraph checkpointer / store.
Business tables: agent / agent_config_history / session / mcp_server.
LangGraph-native tables (checkpoints / checkpoint_writes / checkpoint_blobs / store)
are created by AsyncPostgresSaver/Store `.setup()`.

Design (plan §1, 方案 B):
- agent.id 全程稳定不变; 更新 = in-place 覆写 config_json + 追加 agent_config_history 快照。
- 更新时该 agent 的全部 active 会话软归档（checkpoint 保留 → 历史仍可浏览；
  stream/resume 对 archived 409，防止旧 checkpoint 在新图上恢复）。配置历史保留。
  （物理删除仅在 agent purge / session purge，不可恢复。）

Notes:
- psycopg3 uses `%s` placeholders (NOT asyncpg's `$1`).
- Native JSON/JSONB adaptation — pass python dict directly, no json.dumps.
- All multi-step ops run inside explicit transactions.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from psycopg.errors import UniqueViolation
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
    temperature     REAL,                                  -- 可空：None=用 provider 默认
    top_p           REAL,                                  -- 可空：None=用 provider 默认
    enable_thinking BOOLEAN NOT NULL DEFAULT false,        -- 思考模式开关（GLM: 注入 thinking={"type":"enabled"}）
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

-- ============================ 知识库（RAG）状态卡 ============================
-- 仅状态/元数据；chunk 文本/向量真值入 Elasticsearch（index = kb_<kb_id>）。
CREATE TABLE IF NOT EXISTS knowledge_base (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          VARCHAR NOT NULL UNIQUE,
    description   TEXT,
    es_index_name VARCHAR NOT NULL,          -- 'kb_<uuid>'，创建时写定
    dims          INT,                        -- 库级嵌入维度：创建时可传（默认=全局 EMBEDDING_DIMS），
                                               -- 是入库/检索嵌入客户端的取值来源（修改=破坏性变更，见
                                               -- KnowledgeService.update_kb 守卫）
    status        VARCHAR NOT NULL DEFAULT 'active',
    creator       VARCHAR NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_document (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id        UUID NOT NULL REFERENCES knowledge_base(id) ON DELETE CASCADE,
    bucket_key   TEXT,                        -- MinIO 对象键；KB_STORE_MINIO=false 时为 NULL
    filename     TEXT NOT NULL,
    mime         TEXT NOT NULL,
    size         BIGINT NOT NULL,
    page_count   INT,
    status       VARCHAR NOT NULL DEFAULT 'pending',  -- pending|indexing|indexed|failed
    chunk_count  INT,
    progress     INT,                                  -- 入库进度 0..100（indexing 期间随批推进；indexed=100）
    chunk_size   INT,                                  -- 解析切块参数（生效值，解析触发时写入；NULL=未解析/历史行→用全局默认）
    chunk_overlap INT,                                 -- 同上
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kbdoc_kb ON kb_document(kb_id);
"""

# Idempotent column migrations for pre-existing tables (CREATE TABLE IF NOT
# EXISTS won't add columns to already-present tables). Each is guarded so it's
# safe to run repeatedly.
_MIGRATIONS = [
    "ALTER TABLE agent ADD COLUMN IF NOT EXISTS model_id UUID",
    "ALTER TABLE agent_config_history ADD COLUMN IF NOT EXISTS model_id UUID",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS temperature REAL",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS top_p REAL",
    "ALTER TABLE model ADD COLUMN IF NOT EXISTS enable_thinking BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS progress INT",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS chunk_size INT",
    "ALTER TABLE kb_document ADD COLUMN IF NOT EXISTS chunk_overlap INT",
]


async def init_business_schema(pool: AsyncConnectionPool) -> None:
    """Create business tables if absent + run column migrations. Safe to repeat."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # 连接池 prepare_threshold=0 会强制预编译，而预编译语句不支持一次执行多条
            # SQL；故把 BUSINESS_DDL 按 ';' 拆成单条逐条执行（DDL 内无字面量分号，安全）。
            for stmt in BUSINESS_DDL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await cur.execute(stmt)
            for stmt in _MIGRATIONS:
                await cur.execute(stmt)
    logger.info("business schema ready (model / agent / agent_config_history / session / mcp_server / upload / knowledge_base / kb_document)")

    # kb_document 同名拦截的并发兜底：加 UNIQUE(kb_id, filename)。先清理历史重复行
    # （保留最新），再加唯一索引；任一步失败仅告警，不阻断启动——应用层查询仍为主防线。
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM kb_document a USING kb_document b "
                        "WHERE a.kb_id = b.kb_id AND a.filename = b.filename "
                        "AND (a.created_at, a.id) < (b.created_at, b.id)"
                    )
                    await cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_kbdoc_kb_filename "
                        "ON kb_document(kb_id, filename)"
                    )
    except Exception:  # noqa: BLE001
        logger.exception("ensure kb_document(kb_id, filename) unique index failed (legacy dupes?)")

    # 会话同用户同名去重的并发兜底：部分唯一索引（仅 active + 非空 title 参与去重，
    # 归档会话豁免 → 归档即释放标题；agent 更新软归档会话同理）。先清理历史重复的
    # active 行（同 user 同 title 保留最新），再加索引；失败仅告警不阻断启动。
    # 注意：未来若增加「取消归档」端点，须做同名预检（索引不会自动放行 re-active）。
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session a USING session b "
                        "WHERE a.user_id = b.user_id AND a.title = b.title "
                        "AND a.status = 'active' AND b.status = 'active' "
                        "AND (a.updated_at, a.id) < (b.updated_at, b.id)"
                    )
                    await cur.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_user_title_active "
                        "ON session(user_id, title) "
                        "WHERE status = 'active' AND title IS NOT NULL AND title <> ''"
                    )
    except Exception:  # noqa: BLE001
        logger.exception("ensure session(user_id, title) active-unique index failed (legacy dupes?)")


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
                try:
                    await cur.execute(
                        "INSERT INTO agent (name, description, config_json, model_id, creator) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "RETURNING id, name, description, config_json, model_id, status, "
                        "creator, updater, created_at, updated_at",
                        (name, description, Json(config_json), model_id, creator),
                    )
                except UniqueViolation:
                    # 并发兜底：service 预检通过后、INSERT 落地前的同名竞态 → 归一 409
                    raise FileExistsError(name)
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
    name: Optional[str] = None,
) -> Optional[dict]:
    """Update agent config + model_id in-place + append history snapshot.

    Side effects (same tx): archive this agent's ACTIVE sessions (soft).
    Checkpoints are kept so /history stays browsable (history is read from the
    checkpointer's messages channel, independent of graph topology); the
    stream/resume endpoints refuse archived sessions (409), so stale checkpoint
    state can never be resumed against the new graph. Titles auto-release via
    the (user_id, title) active-partial unique index.
    Returns None if agent not found. name=None（默认）保持原名，便于 restore_agent
    等不改名的调用方。
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                try:
                    await cur.execute(
                        "UPDATE agent SET config_json = %s, model_id = %s, "
                        "description = COALESCE(%s, description), "
                        "name = COALESCE(%s, name), "
                        "updater = %s, updated_at = now() WHERE id = %s "
                        "RETURNING id, name, description, config_json, model_id, status, "
                        "creator, updater, created_at, updated_at",
                        (Json(config_json), model_id, description, name, updater, agent_id),
                    )
                except UniqueViolation:
                    # 并发改名兜底：预检无冲突后、UPDATE 落地前名字被他方占用 → 409。
                    # 仅 name 实际变更时才可能触发（name=None 保持原名不参与竞争）。
                    raise FileExistsError(name)
                agent = await cur.fetchone()
                if agent is None:
                    return None

                await cur.execute(
                    "INSERT INTO agent_config_history (agent_id, config_json, model_id, action, operator) "
                    "VALUES (%s, %s, %s, 'update', %s)",
                    (agent_id, Json(config_json), model_id, updater),
                )

                # soft-archive this agent's active sessions (checkpoints kept)
                await cur.execute(
                    "UPDATE session SET status = 'archived', updated_at = now() "
                    "WHERE agent_id = %s AND status = 'active'",
                    (agent_id,),
                )
                archived = cur.rowcount
    if archived:
        logger.info(
            "agent %s updated; archived %d active sessions (checkpoints kept)",
            agent_id, archived,
        )
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
    unbuildable (sessions get archived by update_agent, but the agent itself
    must stay runnable).
    """
    snapshot = await get_history_snapshot(pool, agent_id, history_id)
    if snapshot is None:
        return None
    return await update_agent(
        pool, agent_id, snapshot["config_json"], snapshot["model_id"],
        description=None, updater=operator,
    )


async def list_agents(pool: AsyncConnectionPool, include_archived: bool = False) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            where = "" if include_archived else " WHERE a.status != 'archived'"
            await cur.execute(
                f"SELECT a.id, a.name, a.description, a.model_id, m.name AS model_name, "
                f"a.status, a.creator, a.updater, a.created_at, a.updated_at "
                f"FROM agent a LEFT JOIN model m ON m.id = a.model_id{where} "
                f"ORDER BY a.updated_at DESC"
            )
            rows = await cur.fetchall()
    return rows


async def get_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT a.id, a.name, a.description, a.config_json, a.model_id, "
                "m.name AS model_name, a.status, a.creator, a.updater, "
                "a.created_at, a.updated_at "
                "FROM agent a LEFT JOIN model m ON m.id = a.model_id WHERE a.id = %s",
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


async def delete_agent(pool: AsyncConnectionPool, agent_id: uuid.UUID) -> bool:
    """Physically delete an agent + cascade (sessions, config history, checkpoints).

    Irreversible (vs archive_agent). Returns True if the agent row was deleted,
    False if not found.
    """
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT thread_id FROM session WHERE agent_id = %s", (agent_id,)
                )
                thread_ids = [r[0] for r in await cur.fetchall()]
                await cur.execute("DELETE FROM session WHERE agent_id = %s", (agent_id,))
                await cur.execute(
                    "DELETE FROM agent_config_history WHERE agent_id = %s", (agent_id,)
                )
                if thread_ids:
                    for tbl in _CHECKPOINT_TABLES:
                        await cur.execute(
                            f"DELETE FROM {tbl} WHERE thread_id = ANY(%s)", (thread_ids,)
                        )
                await cur.execute("DELETE FROM agent WHERE id = %s", (agent_id,))
                deleted = cur.rowcount > 0
    if deleted:
        logger.info("agent %s hard-deleted; purged %d sessions", agent_id, len(thread_ids))
    return deleted


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
    """Insert a session row.

    UniqueViolation → 归一化异常（service 预检与 INSERT 之间竞态的兜底）：
    - idx_session_user_title_active：同用户活跃会话标题撞车 → FileExistsError(409)
    - session_thread_id_key：自传 thread_id 撞车 → ValueError(400)
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO session (agent_id, user_id, thread_id, title) "
                    "VALUES (%s, %s, %s, %s) "
                    "RETURNING id, agent_id, user_id, thread_id, title, status, created_at, updated_at",
                    (agent_id, user_id, thread_id, title),
                )
                row = await cur.fetchone()
            except UniqueViolation as e:
                constraint = getattr(e.diag, "constraint_name", "") or ""
                if constraint == "session_thread_id_key":
                    raise ValueError(f"thread_id '{thread_id}' 已被其它会话使用")
                # 其余唯一冲突均为 (user_id, title) 活跃会话标题
                raise FileExistsError(title or "")
    return row


async def get_active_session_by_user_title(
    pool: AsyncConnectionPool, user_id: str, title: str
) -> Optional[dict]:
    """按 (user_id, title) 查活跃会话（去重预检用；归档会话豁免）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, agent_id, user_id, thread_id, title, status, created_at, updated_at "
                "FROM session WHERE user_id = %s AND title = %s AND status = 'active' "
                "LIMIT 1",
                (user_id, title),
            )
            return await cur.fetchone()


async def get_session_by_thread(
    pool: AsyncConnectionPool, thread_id: str
) -> Optional[dict]:
    """按 thread_id 查会话（自传 thread_id 冲突预检用）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, agent_id, user_id, thread_id, title, status, created_at, updated_at "
                "FROM session WHERE thread_id = %s",
                (thread_id,),
            )
            return await cur.fetchone()


async def list_sessions_by_user(
    pool: AsyncConnectionPool, user_id: str, include_archived: bool = False
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            extra = "" if include_archived else " AND s.status != 'archived'"
            await cur.execute(
                f"SELECT s.id, s.agent_id, s.user_id, s.thread_id, s.title, s.status, "
                f"s.created_at, s.updated_at, a.name AS agent_name "
                f"FROM session s JOIN agent a ON a.id = s.agent_id "
                f"WHERE s.user_id = %s{extra} ORDER BY s.updated_at DESC",
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


async def delete_session(pool: AsyncConnectionPool, session_id: uuid.UUID) -> bool:
    """Physically delete a session + its LangGraph checkpoint state. Irreversible."""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT thread_id FROM session WHERE id = %s", (session_id,)
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                thread_id = row[0]
                await cur.execute("DELETE FROM session WHERE id = %s", (session_id,))
                if thread_id:
                    for tbl in _CHECKPOINT_TABLES:
                        await cur.execute(
                            f"DELETE FROM {tbl} WHERE thread_id = %s", (thread_id,)
                        )
    logger.info("session %s hard-deleted", session_id)
    return True


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


async def get_mcp_server(
    pool: AsyncConnectionPool, server_id: uuid.UUID
) -> Optional[dict]:
    """Fetch one MCP server registration by id."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server WHERE id = %s",
                (server_id,),
            )
            return await cur.fetchone()


async def get_mcp_server_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    """Row by unique name — create/update name-conflict pre-checks."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, kind, config_json, enabled, created_at "
                "FROM mcp_server WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()


async def list_agent_configs(pool: AsyncConnectionPool) -> list[dict]:
    """All agents' {id, name, config_json} — MCP 引用扫描用（改名/删除前置校验）。

    config_json 任意层级的 *tool_names（tool_names / grader_tool_names /
    subagents[*].tool_names ...）均按名引用 `{server}__{tool}` 命名空间。含
    archived（与 list_agents_by_model 同口径：解除归档后工具必须仍可用）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, name, config_json FROM agent")
            return await cur.fetchall()


async def insert_mcp_server(
    pool: AsyncConnectionPool,
    name: str,
    kind: str,
    config_json: dict,
    enabled: bool,
) -> dict:
    """Insert a new MCP server registration.

    UniqueViolation → FileExistsError：应用层 check-then-insert 之间的并发竞态
    由 mcp_server.name UNIQUE 约束兜住，路由层统一映射 409（对齐 kb_document）。
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "INSERT INTO mcp_server (name, kind, config_json, enabled) "
                    "VALUES (%s, %s, %s, %s) "
                    "RETURNING id, name, kind, config_json, enabled, created_at",
                    (name, kind, Json(config_json), enabled),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


async def update_mcp_server(
    pool: AsyncConnectionPool,
    server_id: uuid.UUID,
    name: str,
    kind: str,
    config_json: dict,
    enabled: bool,
) -> Optional[dict]:
    """Update an MCP server registration by PK (name may change).

    取代旧的按名 upsert（改名会 INSERT 新行残留旧行；撞名会静默覆写他人配置）。
    UniqueViolation → FileExistsError 为并发改名兜底。Returns None if absent.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    "UPDATE mcp_server SET name = %s, kind = %s, config_json = %s, "
                    "enabled = %s WHERE id = %s "
                    "RETURNING id, name, kind, config_json, enabled, created_at",
                    (name, kind, Json(config_json), enabled, server_id),
                )
                return await cur.fetchone()
            except UniqueViolation:
                raise FileExistsError(name)


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
    upload_id: Optional[uuid.UUID] = None,
) -> dict:
    """Insert an upload metadata row. kind/mime resolved server-side at upload.

    upload_id: optional explicit PK. The upload layer sets it equal to the MinIO
    bucket_key so the DB-less wrapper can fetch the object by upload_id alone.
    """
    cols = "session_id, user_id, bucket_key, filename, mime, kind, size, page_count, preview"
    placeholders = "%s, %s, %s, %s, %s, %s, %s, %s, %s"
    params: list = [session_id, user_id, bucket_key, filename, mime, kind, size, page_count, preview]
    if upload_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [upload_id, *params]
    sql = (
        f"INSERT INTO upload ({cols}) VALUES ({placeholders}) "
        "RETURNING id, session_id, user_id, bucket_key, filename, mime, kind, size, "
        "page_count, preview, created_at"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
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


async def get_uploads_by_ids(
    pool: AsyncConnectionPool,
    upload_ids: list[uuid.UUID],
    session_id: uuid.UUID,
) -> dict[str, dict]:
    """Batch-fetch upload metadata by id, hard-scoped to a session.

    Used by history rendering: upload_ids come from HumanMessage
    additional_kwargs (written at send time), never parsed from content.
    Returns ``{str(id): row}`` for O(1) lookup by the caller. Scoping by
    session_id guarantees a stale id from another session never leaks.
    """
    if not upload_ids:
        return {}
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, filename, mime, kind, size, page_count, preview "
                "FROM upload WHERE id = ANY(%s) AND session_id = %s",
                (upload_ids, session_id),
            )
            rows = await cur.fetchall()
    return {str(r["id"]): r for r in rows}


# ============================ Knowledge base ops ==========================
# 仅状态/元数据 CRUD（PG 状态卡）；chunk 文本/向量检索归 Elasticsearch。

_KB_COLUMNS = (
    "id, name, description, es_index_name, dims, status, creator, created_at, updated_at"
)
# JOIN 场景需限定表别名：kb_document 同样含 id/created_at/updated_at，
# 直接用 _KB_COLUMNS 会 AmbiguousColumn。结果列名仍为 id/name/...（dict 键不变）。
_KB_COLUMNS_QUALIFIED = ", ".join(f"kb.{c}" for c in _KB_COLUMNS.split(", "))
_KBDOC_COLUMNS = (
    "id, kb_id, bucket_key, filename, mime, size, page_count, "
    "status, chunk_count, progress, chunk_size, chunk_overlap, error, "
    "created_at, updated_at"
)


async def create_kb(
    pool: AsyncConnectionPool,
    name: str,
    description: Optional[str],
    es_index_name: str,
    dims: Optional[int],
    creator: str,
    kb_id: Optional[uuid.UUID] = None,
) -> dict:
    """Create a knowledge base state card. es_index_name='kb_<id>' / dims=global
    EMBEDDING_DIMS are written at creation (dims recorded for drift detection only).

    kb_id: optional explicit PK. Set so es_index_name = 'kb_<id>' is consistent
    (index name embeds the id; resolving both at the service layer avoids a
    placeholder-then-update round trip).
    """
    cols = "name, description, es_index_name, dims, creator"
    placeholders = "%s, %s, %s, %s, %s"
    params: list = [name, description, es_index_name, dims, creator]
    if kb_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [kb_id, *params]
    sql = (
        f"INSERT INTO knowledge_base ({cols}) VALUES ({placeholders}) "
        f"RETURNING {_KB_COLUMNS}"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def get_kb(pool: AsyncConnectionPool, kb_id: uuid.UUID) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE id = %s",
                (kb_id,),
            )
            return await cur.fetchone()


async def get_kb_by_name(pool: AsyncConnectionPool, name: str) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE name = %s",
                (name,),
            )
            return await cur.fetchone()


async def get_kbs_by_ids(
    pool: AsyncConnectionPool, kb_ids: list[uuid.UUID]
) -> list[dict]:
    """Fetch selected KBs by id (stream.py KB anchor injection: needs id+name)."""
    if not kb_ids:
        return []
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS} FROM knowledge_base WHERE id = ANY(%s)",
                (kb_ids,),
            )
            return await cur.fetchall()


async def list_kbs(
    pool: AsyncConnectionPool, include_inactive: bool = False
) -> list[dict]:
    """List knowledge bases with aggregated doc_count / chunk_count."""
    where = "" if include_inactive else " WHERE kb.status = 'active'"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KB_COLUMNS_QUALIFIED}, "
                f"COUNT(d.id) AS doc_count, COALESCE(SUM(d.chunk_count), 0) AS chunk_count "
                f"FROM knowledge_base kb LEFT JOIN kb_document d ON d.kb_id = kb.id "
                f"{where} "
                f"GROUP BY kb.id ORDER BY kb.updated_at DESC"
            )
            return await cur.fetchall()


async def update_kb(
    pool: AsyncConnectionPool,
    kb_id: uuid.UUID,
    name: str,
    description: Optional[str],
    dims: Optional[int] = None,
) -> Optional[dict]:
    """dims 用 COALESCE：传 None 保持原值（服务层守卫后才会传入新维度）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE knowledge_base SET name = %s, "
                f"description = COALESCE(%s, description), "
                f"dims = COALESCE(%s, dims), updated_at = now() "
                f"WHERE id = %s RETURNING {_KB_COLUMNS}",
                (name, description, dims, kb_id),
            )
            return await cur.fetchone()


async def delete_kb(pool: AsyncConnectionPool, kb_id: uuid.UUID) -> bool:
    """Delete a KB row; CASCADE removes its documents. ES index drop is the caller's
    responsibility (delete_kb in KnowledgeService) since DB layer knows nothing of ES."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM knowledge_base WHERE id = %s", (kb_id,))
            return cur.rowcount > 0


async def create_kb_document(
    pool: AsyncConnectionPool,
    kb_id: uuid.UUID,
    bucket_key: Optional[str],
    filename: str,
    mime: str,
    size: int,
    page_count: Optional[int],
    doc_id: Optional[uuid.UUID] = None,
) -> dict:
    """Insert a document state card (status defaults to 'pending').

    doc_id: optional explicit PK. Set == MinIO bucket_key so ingest can fetch
    the object by doc_id alone when KB_STORE_MINIO=true.
    """
    cols = "kb_id, bucket_key, filename, mime, size, page_count"
    placeholders = "%s, %s, %s, %s, %s, %s"
    params: list = [kb_id, bucket_key, filename, mime, size, page_count]
    if doc_id is not None:
        cols = "id, " + cols
        placeholders = "%s, " + placeholders
        params = [doc_id, *params]
    sql = (
        f"INSERT INTO kb_document ({cols}) VALUES ({placeholders}) "
        f"RETURNING {_KBDOC_COLUMNS}"
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(sql, params)
                return await cur.fetchone()
            except UniqueViolation:
                # 并发兜底：应用层 check 与 insert 之间的竞态由唯一索引兜住 → 同名冲突
                raise FileExistsError(filename)


async def get_kb_document_by_name(
    pool: AsyncConnectionPool, kb_id: uuid.UUID, filename: str
) -> Optional[dict]:
    """按 (kb_id, filename) 查文档（精确匹配，区分大小写）。供上传去重预检。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document "
                "WHERE kb_id = %s AND filename = %s",
                (kb_id, filename),
            )
            return await cur.fetchone()


async def get_kb_document(
    pool: AsyncConnectionPool, doc_id: uuid.UUID
) -> Optional[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document WHERE id = %s",
                (doc_id,),
            )
            return await cur.fetchone()


async def list_kb_documents(
    pool: AsyncConnectionPool, kb_id: uuid.UUID
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_KBDOC_COLUMNS} FROM kb_document WHERE kb_id = %s "
                f"ORDER BY created_at DESC",
                (kb_id,),
            )
            return await cur.fetchall()


async def update_kb_document_status(
    pool: AsyncConnectionPool,
    doc_id: uuid.UUID,
    *,
    status: str,
    chunk_count: Optional[int] = None,
    error: Optional[str] = None,
    page_count: Optional[int] = None,
    progress: Optional[int] = None,
) -> Optional[dict]:
    """Transition a document's status (pending|indexing|indexed|failed).

    chunk_count/page_count/progress: COALESCE keeps existing when None. error is set
    explicitly (pass None to clear a prior failure message). progress 0..100：
    indexing 期间随嵌入批次推进（调用方逐批传入）；indexed 时传 100。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE kb_document SET status = %s, "
                f"chunk_count = COALESCE(%s, chunk_count), error = %s, "
                f"page_count = COALESCE(%s, page_count), "
                f"progress = COALESCE(%s, progress), updated_at = now() "
                f"WHERE id = %s RETURNING {_KBDOC_COLUMNS}",
                (status, chunk_count, error, page_count, progress, doc_id),
            )
            return await cur.fetchone()


async def update_kb_document_parse_params(
    pool: AsyncConnectionPool,
    doc_id: uuid.UUID,
    chunk_size: int,
    chunk_overlap: int,
) -> Optional[dict]:
    """解析触发时持久化切块参数（实际生效值，reindex 沿用 → 重建可复现）。"""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"UPDATE kb_document SET chunk_size = %s, chunk_overlap = %s, "
                f"updated_at = now() WHERE id = %s RETURNING {_KBDOC_COLUMNS}",
                (chunk_size, chunk_overlap, doc_id),
            )
            return await cur.fetchone()


async def reset_kb_documents(
    pool: AsyncConnectionPool, kb_id: uuid.UUID
) -> int:
    """维度变更后重置该库全部文档状态卡：status→pending、清 chunk_count/progress/error。

    只 UPDATE 不删除：PG 行与 MinIO 原始留底全部保留，用户可按新维度手动重新解析
    （保证后续可用）。ES 索引已由调用方整体 drop（旧维度向量不可复用）。返回受影响行数。
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE kb_document SET status = 'pending', chunk_count = NULL, "
                "progress = NULL, error = NULL, updated_at = now() "
                "WHERE kb_id = %s",
                (kb_id,),
            )
            return cur.rowcount


async def delete_kb_document(pool: AsyncConnectionPool, doc_id: uuid.UUID) -> bool:
    """Delete a document row. ES chunk removal is the caller's responsibility."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM kb_document WHERE id = %s", (doc_id,))
            return cur.rowcount > 0


# ============================ Model ops ==================================

_MODEL_COLUMNS = (
    "id, name, provider_kind, model_type, base_url, supports_vision, "
    "temperature, top_p, enable_thinking, enabled, created_at, updated_at"
)


async def insert_model(
    pool: AsyncConnectionPool,
    name: str,
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: Optional[str],
    supports_vision: bool,
    temperature: Optional[float],
    top_p: Optional[float],
    enable_thinking: bool,
    enabled: bool,
) -> dict:
    """Insert a registered model definition. api_key stored at the row level."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"INSERT INTO model (name, provider_kind, model_type, api_key, base_url, "
                    f"supports_vision, temperature, top_p, enable_thinking, enabled) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_MODEL_COLUMNS}",
                    (name, provider_kind, model_type, api_key, base_url, supports_vision,
                     temperature, top_p, enable_thinking, enabled),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                # 并发兜底：service 预检通过后、INSERT 落地前的同名竞态 → 归一 409
                raise FileExistsError(name)
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
    temperature: Optional[float],
    top_p: Optional[float],
    enable_thinking: bool,
    enabled: bool,
) -> Optional[dict]:
    """Update a registered model's mutable fields. model_id (PK) never changes.

    api_key=None means "keep existing"; otherwise overwrite (key rotation).
    """
    set_api_key = "api_key = %s" if api_key is not None else "api_key = model.api_key"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = (
                [name, provider_kind, model_type, base_url, supports_vision,
                 temperature, top_p, enable_thinking, enabled]
                + ([api_key] if api_key is not None else [])
                + [model_id]
            )
            try:
                await cur.execute(
                    f"UPDATE model SET name = %s, provider_kind = %s, model_type = %s, "
                    f"base_url = %s, supports_vision = %s, temperature = %s, top_p = %s, "
                    f"enable_thinking = %s, enabled = %s, {set_api_key}, "
                    f"updated_at = now() WHERE id = %s RETURNING {_MODEL_COLUMNS}",
                    tuple(params),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                # 并发改名兜底：预检无冲突后、UPDATE 落地前名字被他方占用 → 409
                raise FileExistsError(name)
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
