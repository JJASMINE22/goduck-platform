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
from app.services.jev_client import JevAuditSink
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
        jev_audit: Optional[JevAuditSink] = None,
    ):
        self.pool = pool
        self.settings = settings
        self.checkpointer = checkpointer
        self.store = store
        self.mcp = mcp_lifecycle
        self.model_store = model_store
        self.jev_audit = jev_audit
        self._cache: OrderedDict[str, tuple[CompiledStateGraph, float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def _jev_capable(self) -> bool:
        """平台级 Jev 能力判定（双出口共用）：开关 ∧ key ∧ 审计 drainer 就绪。"""
        s = self.settings
        return bool(s.jev_enabled and s.typesafe_api_key and self.jev_audit is not None)

    def _jev_gate_factory(self, agent_id: uuid.UUID):
        """Jev 守卫工厂（能力缺失时返回 None，构建回落 {'execute': True} 现状
        行为）。agent_id 烘焙进闭包供审计列。"""
        if not self._jev_capable():
            return None
        from app.core.jev_gate import build_shell_guard

        s = self.settings
        return lambda: build_shell_guard(s, self.jev_audit.submit, agent_id)

    def _jev_rubric_factory(self, agent_id: uuid.UUID):
        """Jev rubric 门控工厂（能力缺失/jev_rubric_enabled=false 时返回 None，
        构建回落 llm 路径 + build_rubric_middleware 侧 warning）。

        签名 factory(rubric_cfg) -> RubricJudge：节点级 questions/thresholds 在
        消费点经 rubric_cfg 渲染（两级烘焙——平台参数随工厂闭包，节点参数随
        消费点），同一 agent 树中 llm/jev 子代理可混布。"""
        s = self.settings
        if not (self._jev_capable() and s.jev_rubric_enabled):
            return None
        from app.core.core_utils import render_jev_criteria
        from app.core.jev_gate import build_rubric_judge

        audit_submit = self.jev_audit.submit

        def factory(rubric_cfg):
            questions, crit_meta = render_jev_criteria(
                rubric_cfg.criteria or [], rubric_cfg.pass_threshold
            )
            return build_rubric_judge(s, audit_submit, agent_id, questions, crit_meta)

        return factory

    def _ctx(self, model, agent_id: Optional[uuid.UUID] = None) -> BuildContext:
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
            jev_gate_factory=self._jev_gate_factory(agent_id) if agent_id else None,
            jev_rubric_factory=self._jev_rubric_factory(agent_id) if agent_id else None,
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
            ctx=self._ctx(model, agent_id=agent_id),
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
