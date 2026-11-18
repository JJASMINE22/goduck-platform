"""Dependency injection: db pool / model / checkpointer / store / mcp / registry.

Replaces the old `base_confs` module-level global singletons. All shared state
lives on `app.state` and is exposed via FastAPI `Depends`.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres import AsyncPostgresStore
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings
from app.core.user_scoped_backend import rename_workspace_dirs, workspace_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User identity (migrated from scripts/pg_compiled_subagent_test_run.SimpleUser)
# ---------------------------------------------------------------------------
class SimpleUser:
    """Minimal BaseUser-protocol implementation for store namespace isolation.

    ``identity`` 恒为 user_id（uuid）：会话/记忆命名空间的稳定隔离键。
    ``workspace_key`` 预组合为 ``{username}-{uuid}``，仅文件系统侧目录命名
    消费（CJK 用户名放行；含空格等非路径安全字符时回退 uuid）。
    """

    def __init__(self, identity: str, user_name: str | None = None):
        self._identity = identity
        self._workspace_key = workspace_key(user_name, identity)

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def workspace_key(self) -> str:
        return self._workspace_key

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
        return iter(["identity", "workspace_key", "is_authenticated", "display_name", "permissions"])


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
        self.vision_describer = None
        # 回合执行注册表（吸收原 StreamTracker 的 withdraw/双发闸；lifespan 装配）
        self.run_manager = None
        # 会话排队发送（queued-send；lifespan 装配）
        self.queue_service = None
        # cron scheduler — None when cron_enabled=false (lifespan wires it)
        self.cron_scheduler = None
        # knowledge base (RAG) — None when kb_enabled=false (lazy, 503-gated)
        # 向量模型注册表（embedding 表行 + 客户端缓存）；嵌入客户端由
        # vector_factory / embedding_store 双源构建（注册行优先，legacy env 回落）
        self.embedding_store = None
        self.vector_factory = None
        self.knowledge_service = None
        self.ingest_pool = None
        # Jev shell 守卫审计 sink — None when jev 未启用/key 缺失（lifespan 装配）
        self.jev_audit = None

    async def startup(self) -> None:
        s = self.settings

        # Postgres pool — shared by business tables + checkpointer + store.
        # TCP keepalive + checkout 健康检查 + 定期换血：防宿主休眠/Docker 暂停等
        # 造成半开僵尸连接（归还时 rollback 挂死 → 池 worker 耗尽 → 全站
        # PoolTimeout 30s，登录/注册等全部无响应）。
        self.pool = AsyncConnectionPool(
            conninfo=make_conninfo(
                s.database_url,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=5,
                keepalives_count=3,
            ),
            max_size=s.db_pool_max_size,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            check=AsyncConnectionPool.check_connection,
            max_lifetime=1800,
            open=False,
        )
        await self.pool.open()

        # LangGraph checkpointer (native tables)
        self.checkpointer = AsyncPostgresSaver(conn=self.pool)
        await self.checkpointer.setup()

        # LangGraph store (per-user memory namespace)
        self.store = AsyncPostgresStore(conn=self.pool)
        await self.store.setup()

        # Business tables (app_user / auth_request / auth_session / agent / ...)
        from app.models import db as dbmod
        await dbmod.init_business_schema(self.pool)

        # 用户体系：env 引导建户（create-if-absent）→ 存量 VARCHAR user_id
        # 对齐（幂等；映射缺失/未覆盖 fail-visible 抛错，阻断启动防脏数据）
        from app.services.auth_service import AuthService, parse_legacy_user_map

        await AuthService(self.pool, self.settings).bootstrap_users()
        mapping: dict[str, "uuid.UUID"] = {}
        for old, username in parse_legacy_user_map(self.settings.legacy_user_map).items():
            row = await dbmod.get_user_by_username(self.pool, username)
            if row is None:
                raise RuntimeError(
                    f"LEGACY_USER_MAP 指向的用户 '{username}' 不存在（检查 BOOTSTRAP_USERS）"
                )
            mapping[old] = row["id"]
        await dbmod.migrate_legacy_user_ids(
            self.pool, mapping, self.settings.workspace_dir
        )

        # 工作区目录可读化：uuid → {username}-{uuid}（幂等 best-effort，
        # 回退用户/无目录用户自动跳过；记忆/会话隔离键不受影响）
        rename_workspace_dirs(
            await dbmod.list_users(self.pool), s.workspace_dir
        )

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


def get_ws_appstate(ws: WebSocket) -> AppState:
    """WebSocket 侧的 AppState 访问器（WS 端点无 Request 依赖注入）。"""
    return ws.app.state.appstate


# ---------------------------------------------------------------------------
# 身份依赖（登录体系）：业务端点的 user_id 一律由此推导，客户端不再传
# ---------------------------------------------------------------------------
SESSION_COOKIE = "goduck_session"


def _auth_service(request: Request):
    from app.services.auth_service import AuthService

    st = get_appstate(request)
    return AuthService(st.pool, st.settings)


async def get_current_user(request: Request) -> dict:
    """cookie 会话令牌 → 有效用户（401 族：未登录/过期/停用）。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    user = await _auth_service(request).validate_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user


async def require_manager(user: dict = Depends(get_current_user)) -> dict:
    """管理资源（智能体/模型/技能/MCP/知识库 写操作）的门禁。"""
    if not user["is_manager"]:
        raise HTTPException(status_code=403, detail="该操作需要管理员权限")
    return user


def get_pool(request: Request) -> AsyncConnectionPool:
    return get_appstate(request).pool


def get_checkpointer(request: Request) -> AsyncPostgresSaver:
    return get_appstate(request).checkpointer


def get_store(request: Request) -> AsyncPostgresStore:
    return get_appstate(request).store


def get_settings_dep(request: Request) -> Settings:
    return get_appstate(request).settings


def get_knowledge_service(request: Request):
    """知识库服务访问器（kb_enabled=false 时为 None；路由层自行 503）。"""
    return get_appstate(request).knowledge_service
