"""HTTP HITL resolver (plan §5.3).

Mirrors the CLI decision format (hitl_utils.collect_cli_decisions) but the
decision source is an HTTP request body instead of stdin. Both HITL origins
(declarative interrupt_on / interrupt() primitive) are handled identically.

Resume contract (plan §11.1):
- resume MUST verify the session is actually in an interrupt state, else 409.
"""
from __future__ import annotations

import uuid
from typing import Optional

from langgraph.graph.state import CompiledStateGraph


class NotInterruptedError(Exception):
    """Raised when resume is called but the graph is not in an interrupt state."""


class HITLResolver:
    """Validate interrupt state + format decisions for Command(resume=...)."""

    async def assert_interrupted(
        self, agent: CompiledStateGraph, config: dict
    ) -> Optional[dict]:
        """Return interrupt payload if currently paused, else None."""
        state = await agent.aget_state(config)
        if not state.next:
            return None
        for task in getattr(state, "tasks", []):
            if getattr(task, "interrupts", None):
                return _first_interrupt_value(task)
        return None

    def normalize_decisions(self, decisions: list) -> list[dict]:
        """Convert API Decision models to the dict format deepagents expects."""
        out: list[dict] = []
        for d in decisions:
            data = d.model_dump(exclude_none=True) if hasattr(d, "model_dump") else dict(d)
            # ensure required shape for Command(resume={"decisions":[...]})
            out.append(data)
        return out


def _first_interrupt_value(task) -> dict:
    for intr in getattr(task, "interrupts", []) or []:
        val = intr.value if hasattr(intr, "value") else intr.get("value")
        if isinstance(val, dict):
            return val
    return {}
