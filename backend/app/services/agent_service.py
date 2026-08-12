"""Agent storage + validation + topology rendering (plan §3 / §4).

Validation 100% reuses AgentConfig.model_validate (no rule rewrites).
Rendering reuses build_delegation_mermaid (AgentConfig as single source of truth).
"""
from __future__ import annotations

import uuid
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.core.schema_utils import AgentConfig, config_to_yaml, parse_config
from app.core.topology_utils import build_delegation_mermaid
from app.models import db as dbmod


def _flatten_errors(e: Exception) -> list[str]:
    errs = []
    if hasattr(e, "errors"):
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", []))
            errs.append(f"{loc}: {err.get('msg')}" if loc else err.get("msg"))
    if not errs:
        errs = [str(e)]
    return errs


class AgentService:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    # -------- validation + preview --------
    def validate_config(self, config: dict) -> tuple[bool, Optional[list[str]]]:
        """Return (valid, errors). Reuses AgentConfig.model_validate."""
        try:
            parse_config(config)
            return True, None
        except Exception as e:
            return False, _flatten_errors(e)

    def render_mermaid(self, config: dict) -> Optional[str]:
        """Build delegation-tree mermaid text from a config dict (best-effort)."""
        try:
            agent_config = parse_config(config)
            return build_delegation_mermaid(agent_config)
        except Exception:
            return None

    # -------- agent CRUD --------
    async def create(
        self,
        name: str,
        description: Optional[str],
        config: dict,
        model_id: uuid.UUID,
        creator: str,
    ) -> dict:
        valid, errs = self.validate_config(config)
        if not valid:
            raise ValueError("; ".join(errs))  # router maps ValueError -> 400
        if await dbmod.get_agent_by_name(self.pool, name) is not None:
            raise FileExistsError(name)  # router maps -> 409
        return await dbmod.create_agent(self.pool, name, description, config, model_id, creator)

    async def update(
        self,
        agent_id: uuid.UUID,
        config: dict,
        model_id: uuid.UUID,
        description: Optional[str],
        updater: str,
        name: Optional[str] = None,
    ) -> dict:
        valid, errs = self.validate_config(config)
        if not valid:
            raise ValueError("; ".join(errs))
        existing = await dbmod.get_agent(self.pool, agent_id)
        if existing is None:
            raise KeyError(f"agent {agent_id} not found")
        # 改名去重：仅当传入新名字且与当前名不同时校验；命中"其它"智能体即冲突（router→409）。
        # agent.name 上已有 UNIQUE 约束，此处前置校验是为了返回语义化 409 而非 IntegrityError→500。
        if name is not None and name != existing["name"]:
            owner = await dbmod.get_agent_by_name(self.pool, name)
            if owner is not None and owner["id"] != agent_id:
                raise FileExistsError(name)
        return await dbmod.update_agent(
            self.pool, agent_id, config, model_id, description, updater, name
        )

    async def restore(self, agent_id: uuid.UUID, history_id: uuid.UUID, operator: str) -> Optional[dict]:
        return await dbmod.restore_agent(self.pool, agent_id, history_id, operator)

    async def history_snapshot(self, agent_id: uuid.UUID, history_id: uuid.UUID) -> Optional[dict]:
        """Peek a history snapshot's {config_json, model_id} without committing.

        Lets the router validate the candidate model_id BEFORE restore_agent's
        destructive commit (session/checkpoint purge).
        """
        return await dbmod.get_history_snapshot(self.pool, agent_id, history_id)

    async def list_agents(self, include_archived: bool = False) -> list[dict]:
        return await dbmod.list_agents(self.pool, include_archived=include_archived)

    async def get(self, agent_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.get_agent(self.pool, agent_id)

    async def archive(self, agent_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.archive_agent(self.pool, agent_id)

    async def delete(self, agent_id: uuid.UUID) -> bool:
        """Hard delete (physical) + cascade sessions/checkpoints/history. Irreversible."""
        return await dbmod.delete_agent(self.pool, agent_id)

    async def list_history(self, agent_id: uuid.UUID) -> list[dict]:
        return await dbmod.list_history(self.pool, agent_id)

    async def export_yaml(self, agent_id: uuid.UUID) -> Optional[str]:
        agent = await dbmod.get_agent(self.pool, agent_id)
        if agent is None:
            return None
        return config_to_yaml(agent["config_json"])
