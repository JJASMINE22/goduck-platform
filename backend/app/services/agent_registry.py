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
from app.services.mcp_lifecycle import MCPLifecycle, TOOL_SEP
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

    async def _shell_allow_pkgs(self) -> frozenset[str]:
        """在册 MCP 前置依赖包名集（mcp_server 表 → command/args 解析）。

        读取失败回落空集（L1 扩展面中在册包直通退化为不匹配，其余面不受
        影响）——构建绝不因数据源故障而失败。
        """
        if not (self._jev_capable() and self.settings.jev_shell_l1_allow):
            return frozenset()
        from app.core.jev_gate import parse_prereq_packages

        try:
            rows = await dbmod.list_mcp_servers(self.pool)
        except Exception:
            logger.exception("jev shell guard: load mcp servers failed (empty allow set)")
            return frozenset()
        return parse_prereq_packages(rows)

    def _jev_gate_factory(self, agent_id: uuid.UUID, allow_pkgs: frozenset[str] = frozenset()):
        """Jev 守卫工厂（能力缺失时返回 None，构建回落 {'execute': True} 现状
        行为）。agent_id 烘焙进闭包供审计列；allow_pkgs 随图构建烘焙（改 MCP
        配置需重启或等 registry TTL 过期——与阈值烘焙同语义）。"""
        if not self._jev_capable():
            return None
        from app.core.jev_gate import build_shell_guard

        s = self.settings
        return lambda: build_shell_guard(s, self.jev_audit.submit, agent_id, allow_pkgs=allow_pkgs)

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

    def _jev_kb_factory(self, agent_id: uuid.UUID):
        """kb 检索门控工厂（能力缺失/jev_kb_rerank_enabled=false 时返回 None，
        构建不挂载，行为=现状）。

        签名 factory() -> JevKbRerankMiddleware：kb 门控无节点级参数（问题集
        为代码常量），消费点（graph_utils 主代理构建）直接实例化。平台级参数
        （client/审计/mode/阈值）随工厂闭包烘焙。plan: jev-kb-rerank-middleware。"""
        s = self.settings
        if not (self._jev_capable() and s.jev_kb_rerank_enabled):
            return None
        from app.core.jev_kb_gate import build_jev_kb_gate

        return lambda: build_jev_kb_gate(s, self.jev_audit.submit, agent_id)

    def _ctx(
        self,
        model,
        agent_id: Optional[uuid.UUID] = None,
        allow_pkgs: frozenset[str] = frozenset(),
        kb_cards: Optional[list] = None,
    ) -> BuildContext:
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
            jev_gate_factory=self._jev_gate_factory(agent_id, allow_pkgs) if agent_id else None,
            jev_rubric_factory=self._jev_rubric_factory(agent_id) if agent_id else None,
            jev_kb_factory=self._jev_kb_factory(agent_id) if agent_id else None,
            kb_cards=kb_cards,
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

    def _wrap_ctx_tools(self, tools: list, agent_id: uuid.UUID) -> list:
        """_gd_ctx 注入包装（两级门：server 行 inject_ctx ∧ 工具 schema 声明）。

        copy-on-wrap：原池对象不动（`_rename_tool` 同款语义），各图持独立
        wrapper 实例（闭包烘焙 agent_id）；wrapper 保留 namespaced 名，三处
        按名筛选点（graph_utils dicted/compiled/dictionary）自动继承包装
        副本，graph_utils 零改动。
        """
        prefixes = tuple(
            f"{name}{TOOL_SEP}" for name in self.mcp.ctx_inject_servers()
        )
        if not prefixes:
            return tools
        from app.mcps.gd_ctx import inject_gd_ctx, needs_ctx

        wrapped, hits = [], 0
        for t in tools:
            if needs_ctx(t) and str(t.name).startswith(prefixes):
                try:
                    wrapped.append(inject_gd_ctx(t, str(agent_id)))
                    hits += 1
                except Exception:  # noqa: BLE001 — 包装失败退回原工具并告警（构建绝不因包装中断）
                    logger.warning("gd_ctx wrap failed tool=%s（退回原工具）", t.name, exc_info=True)
                    wrapped.append(t)
            else:
                wrapped.append(t)
        if hits:
            logger.info("gd_ctx wrapped %d tool(s) for agent %s", hits, agent_id)
        return wrapped

    async def _kb_cards(self, kb_ids: list) -> list:
        """知识集卡片（id/name/description，构建期提示面注入用）。

        现查不快照（与能力派生同哲学）：悬空 id（KB 已删）静默滤除并告警，
        绝不因数据源故障而构建失败。查询失败回落空卡片（提示面退化为无名单）。
        """
        if not kb_ids:
            return []
        try:
            rows = await dbmod.get_kbs_by_ids(self.pool, [uuid.UUID(k) for k in kb_ids])
        except Exception:  # noqa: BLE001
            logger.warning("kb_cards resolve failed (empty fallback)", exc_info=True)
            return []
        found = {str(r["id"]): r for r in rows}
        missing = [k for k in kb_ids if k not in found]
        if missing:
            logger.warning("agent kb_ids 悬空（已删除？）：%s", missing)
        return [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "description": (r.get("description") or "").strip(),
            }
            for r in (found[k] for k in kb_ids if k in found)
        ]

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
        # _gd_ctx 包装先于图组装：各筛选点拿到的即已剥 schema 的副本
        all_tools = self._wrap_ctx_tools(list(await self.mcp.get_tools()), agent_id)
        middleware = [ToolErrorHandlingMiddleware()]

        allow_pkgs = await self._shell_allow_pkgs()
        kb_cards = await self._kb_cards(agent_config.kb_ids or [])
        agent = create_agent_recursive(
            agent_config,
            all_tools,
            middleware=middleware,
            ctx=self._ctx(model, agent_id=agent_id, allow_pkgs=allow_pkgs, kb_cards=kb_cards),
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
