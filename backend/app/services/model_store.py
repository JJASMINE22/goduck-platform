"""Registered-model store + ChatOpenAI factory (mirrors MCPLifecycle).

Loads enabled model definitions from the `model` table at startup, builds and
caches one chat client per model_id, and resolves capability (supports_vision).

Design:
- provider_kind is a "client family" enum, not a vendor name. Today everything
  routes through the single OpenAI-compatible family (ChatOpenAI + base_url).
- The build factory is the single dispatch seam; adding a native-SDK family
  later means one new branch + pip install, no schema change.
- Cache keyed by model_id so key rotation (update) re-uses the build pipeline;
  refresh() rebuilds on demand.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from langchain_core.language_models.chat_models import BaseChatModel
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod

logger = logging.getLogger(__name__)


def build_chat_model(
    provider_kind: str,
    model_type: str,
    api_key: str,
    base_url: Optional[str],
    *,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    enable_thinking: Optional[bool] = None,
) -> BaseChatModel:
    """Construct a chat client by provider family. Single dispatch seam.

    Adding a non-OpenAI-compatible family later = one new branch here + the
    matching langchain-<vendor> package. Schema stays unchanged.

    temperature/top_p 可空（None=用 provider 默认）；
    enable_thinking 三态注入 thinking 请求体（GLM/openai_compatible 约定：
    {"thinking":{"type":"enabled"|"disabled"}}）：
    - None  = 不注入（用 provider 服务端默认）
    - True  = 显式开启（思考模型）
    - False = 显式关闭（GLM V 系列/部分思考模型服务端默认开启，关闭须显式 disabled）
    """
    if provider_kind == "openai_compatible":
        from langchain_openai import ChatOpenAI

        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if enable_thinking is not None:
            # thinking 必须走 extra_body（OpenAI SDK 的"未校验请求体"通道），
            # 不能放进 model_kwargs——后者会被当作 create() 的顶层关键字参数，
            # SDK 校验签名不认 'thinking' → "unexpected keyword argument 'thinking'"。
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled" if enable_thinking else "disabled"}
            }
        # stream_usage：流式请求带 stream_options={"include_usage": true}，使
        # 最后一个 chunk 携带 usage → 聚合后的 AIMessage.usage_metadata 非空
        # （StreamAdapter 据此发 usage 事件、session 表累计 token 消耗）。
        # langchain-openai 仅对官方 OpenAI 端点默认开启；本项目全部走 OpenAI
        # 兼容网关（base_url 恒非空），必须显式开启。GLM/DeepSeek/DashScope
        # compatible-mode 均已支持该参数。
        return ChatOpenAI(
            model=model_type,
            api_key=api_key,
            base_url=base_url or None,
            stream_usage=True,
            stream_chunk_timeout=600.0,
            **kwargs,
        )
    raise ValueError(f"不支持的 provider_kind: {provider_kind}")


class ModelStore:
    """Manage registered model definitions + cached chat clients (DB-driven)."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool
        # model_id (uuid) -> {client, supports_vision, model_type}
        self._models: dict[uuid.UUID, dict] = {}
        self._started = False

    async def startup(self) -> None:
        # langchain-openai 1.x 不透传 reasoning_content，需注入提取（幂等）
        from app.services.thinking_patch import apply_once

        apply_once()
        await self.refresh()
        self._started = True
        logger.info("ModelStore started: %d enabled models", len(self._models))

    async def refresh(self) -> None:
        """Reload enabled models from DB + (re)build clients (fault-isolated)."""
        rows = await dbmod.list_enabled_models(self.pool)
        new_models: dict[uuid.UUID, dict] = {}
        for row in rows:
            mid = row["id"]
            try:
                client = build_chat_model(
                    row["provider_kind"],
                    row["model_type"],
                    row["api_key"],
                    row.get("base_url"),
                    temperature=row.get("temperature"),
                    top_p=row.get("top_p"),
                    enable_thinking=row.get("enable_thinking"),
                )
                entry = {
                    "client": client,
                    "supports_vision": row["supports_vision"],
                    "model_type": row["model_type"],
                    "name": row["name"],
                }
                new_models[mid] = entry
            except Exception as e:  # noqa: BLE001
                logger.warning("model '%s' (%s) build failed (skipped): %s", row.get("name"), mid, e)
        self._models = new_models

    def get(self, model_id: uuid.UUID) -> BaseChatModel:
        """Return the cached chat client for a model_id. Raise if unavailable."""
        entry = self._models.get(model_id)
        if entry is None:
            raise KeyError(
                f"model {model_id} 不可用（未注册 / 已禁用 / 构建失败），请先在 /models 注册并启用"
            )
        return entry["client"]

    def supports_vision(self, model_id: uuid.UUID) -> bool:
        entry = self._models.get(model_id)
        return bool(entry and entry["supports_vision"])

    def has(self, model_id: uuid.UUID) -> bool:
        return model_id in self._models

    def model_type_of(self, model_id: uuid.UUID) -> Optional[str]:
        entry = self._models.get(model_id)
        return entry["model_type"] if entry else None

    async def shutdown(self) -> None:
        self._models.clear()
        self._started = False
        logger.info("ModelStore shut down")
