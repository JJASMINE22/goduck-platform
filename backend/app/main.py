"""FastAPI entry: router registration + lifespan wiring.

Assembles the modular monolith: API layer -> Service layer -> Runtime layer ->
Persistence layer -> Infra layer (plan §三). All shared state on app.state via
Depends, no module-level mutable singletons (plan §11.3.4).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.deps import AppState

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("goduck")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 可观测性：打印本地文件根与挂载点，便于排查 agent write_file 实际落盘位置
    # （/workspace/ 路由 root_dir = local_dir/<agent.workspace>，见 core_utils._build_backend）
    logger.info(
        "local_dir=%s | /workspace/ root=%s | /memories/=%s | /skills/=%s",
        settings.local_dir,
        settings.workspace_dir,
        settings.memories_dir,
        settings.skills_dir,
    )
    st = AppState(settings)
    # 1. infra: pool + checkpointer + store + business tables
    await st.startup()
    # 2. MCP lifecycle (connect DB-registered servers, prewarm tools)
    from app.services.mcp_lifecycle import MCPLifecycle

    st.mcp_lifecycle = MCPLifecycle(settings, pool=st.pool)
    await st.mcp_lifecycle.startup()
    # 3. ModelStore (load DB-registered models, build cached chat clients)
    from app.services.model_store import ModelStore

    st.model_store = ModelStore(pool=st.pool)
    await st.model_store.startup()
    # 4. AgentRegistry (lazy build, depends on checkpointer/store/mcp/model_store)
    from app.services.agent_registry import AgentRegistry

    st.agent_registry = AgentRegistry(
        pool=st.pool,
        settings=settings,
        checkpointer=st.checkpointer,
        store=st.store,
        mcp_lifecycle=st.mcp_lifecycle,
        model_store=st.model_store,
    )
    # 5. StreamAdapter (max_invoke_retries from config)
    from app.services.stream_adapter import StreamAdapter

    st.stream_adapter = StreamAdapter(max_invoke_retries=settings.max_invoke_retries)

    # 6. MinIO storage (附件字节持久层)
    from app.services.storage import build_minio_storage

    st.minio_storage = build_minio_storage(settings)

    # 7. ContentComposer (附件 → HumanMessage.content 组装)
    from app.services.content_composer import ContentComposer

    st.content_composer = ContentComposer(pool=st.pool, minio=st.minio_storage)

    app.state.appstate = st

    # 8. warmup: preload active agents (fault-tolerant — one failure non-fatal)
    from app.models import db as dbmod

    for a in await dbmod.list_active_agents(st.pool):
        try:
            await st.agent_registry.get_or_build(a["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup agent '%s' failed (skipped): %s", a.get("name"), e)

    logger.info("goduck-platform started")
    yield
    # shutdown: reverse-order cleanup
    await st.mcp_lifecycle.shutdown()
    await st.model_store.shutdown()
    await st.shutdown()
    logger.info("goduck-platform stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="goduck-platform",
        version="0.1.0",
        lifespan=lifespan,
        root_path=settings.root_path,
    )

    from app.api import agents, mcp, models, sessions, skills, stream, uploads

    app.include_router(agents.router)
    app.include_router(sessions.router)
    app.include_router(stream.router)
    app.include_router(skills.router)
    app.include_router(mcp.router)
    app.include_router(uploads.router)
    app.include_router(models.router)

    @app.get("/health", tags=["健康检查"])
    async def health():
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=True)
