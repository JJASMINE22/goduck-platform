"""Compiled-graph cache + lazy build.

Caches CompiledStateGraph per agent_id (stable across updates). On update the
caller invalidates the agent_id then rebuilds, so sessions always run against
the latest active config (plan §4, 方案 B).

Concurrency: per-agent asyncio.Lock prevents duplicate concurrent builds.
LRU + TTL bound memory.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Optional

from langgraph.graph.state import CompiledStateGraph
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.core.core_utils import ToolErrorHandlingMiddleware
from app.core.graph_utils import BuildContext, create_agent_recursive
from app.core.schema_utils import AgentConfig
from app.models import db as dbmod
from app.services.mcp_lifecycle import MCPLifecycle
from app.services.model_store import ModelStore

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Lazy-build + cache compiled agents keyed by agent_id."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        settings: Settings,
        checkpointer,
        store,
        mcp_lifecycle: MCPLifecycle,
        model_store: ModelStore,
    ):
        self.pool = pool
        self.settings = settings
        self.checkpointer = checkpointer
        self.store = store
        self.mcp = mcp_lifecycle
        self.model_store = model_store
        self._cache: OrderedDict[str, tuple[CompiledStateGraph, float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def _ctx(self, model) -> BuildContext:
        s = self.settings
        return BuildContext(
            model=model,
            checkpointer=self.checkpointer,
            store=self.store,
            sandbox=None,
            local_dir=str(s.local_dir),
            memories_dir=str(s.memories_dir),
            workspace_dir=str(s.workspace_dir),
            skills_dir=str(s.skills_dir),
        )

    async def get_or_build(self, agent_id: uuid.UUID) -> CompiledStateGraph:
        key = str(agent_id)
        cached = self._cache.get(key)
        if cached is not None and (time.time() - cached[1]) < self.settings.registry_ttl_seconds:
            self._cache.move_to_end(key)
            return cached[0]

        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(key)
            if cached is not None and (time.time() - cached[1]) < self.settings.registry_ttl_seconds:
                return cached[0]
            agent = await self._build(agent_id)
            self._cache[key] = (agent, time.time())
            self._evict()
            return agent

    async def _build(self, agent_id: uuid.UUID) -> CompiledStateGraph:
        agent_row = await dbmod.get_agent(self.pool, agent_id)
        if agent_row is None:
            raise KeyError(f"agent {agent_id} not found")
        agent_config = AgentConfig.model_validate(agent_row["config_json"])

        # model is per-agent, resolved from registered model_id (DB-driven)
        model_id = agent_row.get("model_id")
        if model_id is None:
            raise KeyError(f"agent {agent_id} 缺少 model_id，请先绑定已注册模型")
        model = self.model_store.get(model_id)

        # tools are 100% MCP-driven (namespaced); no local tools (plan §5)
        all_tools = list(await self.mcp.get_tools())
        middleware = [ToolErrorHandlingMiddleware()]

        agent = create_agent_recursive(
            agent_config,
            all_tools,
            middleware=middleware,
            ctx=self._ctx(model),
        )
        logger.info("built agent '%s' (id=%s)", agent_row["name"], agent_id)
        return agent

    def _evict(self) -> None:
        max_entries = self.settings.registry_max_entries
        while len(self._cache) > max_entries:
            self._cache.popitem(last=False)

    def invalidate(self, agent_id: Optional[uuid.UUID] = None) -> None:
        if agent_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(agent_id), None)
