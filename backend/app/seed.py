"""Seed an agent from a YAML template.

Usage (from backend/):
    python -m app.seed agent_confs/default.yaml --name "Default Gateway" \
        --creator admin --model-id <已注册模型的 uuid>

前置：先通过 /models API 注册一个模型，拿到其 id 再传入。
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from app.config import get_settings
from app.deps import AppState
from app.services.agent_service import AgentService


async def seed(yaml_path: str, name: str, description: str | None, creator: str, model_id: uuid.UUID) -> None:
    from app.core.schema_utils import load_config_from_yaml

    config = load_config_from_yaml(yaml_path)
    st = AppState(get_settings())
    await st.startup()
    try:
        svc = AgentService(st.pool)
        agent = await svc.create(name, description, config, model_id, creator)
        print(f"created agent {agent['id']} '{agent['name']}' (active)")
    finally:
        await st.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("yaml_path", help="path to agent_confs/*.yaml template")
    parser.add_argument("--name", required=True)
    parser.add_argument("--creator", required=True)
    parser.add_argument("--model-id", required=True, help="已注册模型的 uuid（先经 /models 注册）")
    parser.add_argument("--description", default=None)
    args = parser.parse_args()
    asyncio.run(seed(args.yaml_path, args.name, args.description, args.creator, uuid.UUID(args.model_id)))


if __name__ == "__main__":
    main()