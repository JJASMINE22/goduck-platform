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

    # 4a. Jev shell 守卫审计（先于 registry：gate factory 需要 submit 回调）。
    #     仅 jev 启用且 key 就绪时启动 drainer；否则 registry 不注入 gate
    #     （构建回落 {"execute": True} 现状行为，健康能力位同步 false）。
    st.jev_audit = None
    jev_capable = settings.jev_enabled and bool(settings.typesafe_api_key)
    if settings.jev_enabled and not settings.typesafe_api_key:
        logger.warning(
            "jev_enabled=true but TYPESAFE_API_KEY missing — shell guard NOT active "
            "(execute HITL stays fully manual)"
        )
    if jev_capable:
        from app.services.jev_client import JevAuditSink

        st.jev_audit = JevAuditSink()
        await st.jev_audit.start(st.pool)
        logger.info(
            "jev shell guard active (mode=%s threshold=%.2f model=%s)",
            settings.jev_mode, settings.jev_safe_threshold, settings.jev_model,
        )

    st.agent_registry = AgentRegistry(
        pool=st.pool,
        settings=settings,
        checkpointer=st.checkpointer,
        store=st.store,
        mcp_lifecycle=st.mcp_lifecycle,
        model_store=st.model_store,
        jev_audit=st.jev_audit,
    )
    # 5. StreamAdapter (max_invoke_retries from config)
    from app.services.stream_adapter import StreamAdapter

    st.stream_adapter = StreamAdapter(max_invoke_retries=settings.max_invoke_retries)

    # 6. MinIO storage (附件字节持久层)
    from app.services.storage import build_minio_storage

    st.minio_storage = build_minio_storage(settings)

    # 7. ContentComposer (附件 → HumanMessage.content 组装) + VisionDescriber
    #    (图像预消费：经已注册视觉模型生成文本描述，thinking 强制 off)
    from app.services.content_composer import ContentComposer
    from app.services.vision_describer import VisionDescriber

    st.vision_describer = VisionDescriber(
        st.model_store, timeout_s=settings.attachment_describe_timeout_s
    )
    st.content_composer = ContentComposer(
        pool=st.pool,
        minio=st.minio_storage,
        describe_timeout_s=settings.attachment_describe_timeout_s,
        parse_concurrency=settings.attachment_parse_concurrency,
        content_max_kb=settings.attachment_content_max_kb,
        describer=st.vision_describer,
    )

    # 8. Knowledge base (RAG) — 仅 kb_enabled=true 时装配（入库/管理写路径）
    if settings.kb_enabled:
        from app.services.ingest_pool import IngestPool
        from app.services.knowledge_service import KnowledgeService
        from app.services.vector_store import VectorStoreFactory

        # 嵌入客户端由工厂按 kb.dims 内部构建+缓存（per-dims），此处不再持单例
        st.vector_factory = VectorStoreFactory(settings)
        st.knowledge_service = KnowledgeService(
            pool=st.pool, settings=settings,
            minio=st.minio_storage, vector_factory=st.vector_factory,
        )
        st.ingest_pool = IngestPool(st.knowledge_service, settings.kb_ingestion_concurrency)
        logger.info("knowledge base enabled (vector_factory/ingest_pool ready)")
    else:
        logger.info("knowledge base disabled (kb_enabled=false)")

    app.state.appstate = st

    # 9. warmup: preload active agents (fault-tolerant — one failure non-fatal)
    from app.models import db as dbmod

    for a in await dbmod.list_active_agents(st.pool):
        try:
            await st.agent_registry.get_or_build(a["id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup agent '%s' failed (skipped): %s", a.get("name"), e)

    # 10. Cron scheduler — DB 驱动 tick 循环（创建/更新任务落库即生效）
    if settings.cron_enabled:
        from app.services.cron_scheduler import CronScheduler

        # kind 隔离引入前的遗留治理：停用仍绑在 chat 型智能体上的任务（幂等，
        # 原因写入 last_error 供用户换绑后重新启用）
        paused = await dbmod.pause_cron_jobs_on_non_cron_agents(st.pool)
        if paused:
            logger.warning("paused %d legacy cron job(s) bound to non-cron agents", paused)

        # shell 禁令存量治理（plan §七.3）：已绑定 allow_shell 智能体的存量任务
        # 仅 WARNING（不自动删除，由用户改绑/关闭 shell）；后续每次触发仍会被
        # 调度器防御复查拦截并计为失败 run（fail-visible）。
        from app.core.schema_utils import tree_allows_shell

        for row in await dbmod.enabled_cron_jobs_with_agent_config(st.pool):
            if tree_allows_shell(row.get("config_json") or {}):
                logger.warning(
                    "cron job '%s' (id=%s) is bound to agent %s whose config enables "
                    "allow_shell — unattended shell is refused; runs will fail until "
                    "the agent's shell capability is removed or the job rebinds",
                    row.get("name"), row.get("id"), row.get("agent_id"),
                )

        st.cron_scheduler = CronScheduler(st)
        await st.cron_scheduler.start()
        logger.info(
            "cron scheduler enabled (tick=%ss, max_concurrency=%d)",
            settings.cron_tick_seconds, settings.cron_max_concurrency,
        )
    else:
        logger.info("cron scheduler disabled (cron_enabled=false)")

    logger.info("goduck-platform started")
    yield
    # shutdown: reverse-order cleanup — 调度器最先停（不再派发新运行），
    # 宽限收割在飞运行后再拆基础设施。
    if st.cron_scheduler is not None:
        await st.cron_scheduler.shutdown()
    if st.ingest_pool is not None:
        await st.ingest_pool.shutdown()
    if st.vector_factory is not None:
        st.vector_factory.close()
    if st.jev_audit is not None:
        await st.jev_audit.shutdown()
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

    from app.api import agents, cron, mcp, models, sessions, skills, stream, uploads, workspace
    from app.models.schemas import HealthResponse

    app.include_router(agents.router)
    app.include_router(sessions.router)
    app.include_router(stream.router)
    app.include_router(skills.router)
    app.include_router(mcp.router)
    app.include_router(uploads.router)
    app.include_router(models.router)
    app.include_router(workspace.router)
    app.include_router(cron.router)

    # 知识库路由：仅 kb_enabled=true 时挂载（避免禁用时仍 import RAG 重依赖；
    # 禁用时 KB 接口 404、会话 knowledge-capability 返回 allow_knowledge=false）。
    if settings.kb_enabled:
        from app.api import knowledge

        app.include_router(knowledge.router)

    # 语音路由：仅 voice_enabled=true 且已配 AssemblyAI key 时挂载（能力位同步下发）。
    # key 缺失属部署错误：降级为不挂载 + 警告（健康位 false），不让启动崩溃。
    if settings.voice_enabled:
        if settings.assemblyai_api_key:
            from app.api import voice

            app.include_router(voice.router)
            logger.info("voice input enabled (assemblyai stt ready)")
        else:
            logger.warning(
                "voice_enabled=true but ASSEMBLYAI_API_KEY missing — voice routes NOT mounted"
            )

    @app.get("/health", tags=["健康检查"], response_model=HealthResponse)
    async def health():
        # capabilities 前端零配置感知全局能力开关（kb_enabled 等），
        # 驱动菜单/路由/页面可见性（F2）。单一真相源 = settings。
        return {
            "status": "ok",
            "capabilities": {
                "kb_enabled": settings.kb_enabled,
                "voice_enabled": settings.voice_enabled and bool(settings.assemblyai_api_key),
                "jev_enabled": settings.jev_enabled and bool(settings.typesafe_api_key),
            },
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app", 
        host=settings.app_host, 
        port=settings.app_port, 
        reload_dirs=["app"],
        reload=True
    )
