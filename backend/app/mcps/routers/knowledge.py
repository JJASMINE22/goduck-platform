"""kb_search 工具（自 mcp-servers 网关收敛入 backend 进程，plan W8）。

v2 语义：知识集升格 agent 配置项（AgentConfig.kb_ids）——服务端按
`_gd_ctx.agent_id` 现查 agent 行，校验 LLM 传入的 kb_ids ⊆ agent 知识集
（集合外 = 按编造拒绝，错误信封列出可用集）；检索本体委托
KnowledgeService.search（向量模型/维度感知分组 + 轮转归并的单一实现，
消灭旧网关与 backend 的双份检索代码）。

wire 契约与旧网关对齐（前端 KbSearchToolCard 消费，name 匹配
`{server}__kb_search`）：
  {result, query, total, hits: [{content, score, filename, doc_id, page,
   chunk_index, kb_id, heading_path}]}

锚定元数据（doc_url/bbox/assets）不随 LLM 返回：前端持 hits 键组经
/knowledge-bases/anchor-cards 懒取——本进程持有 MinIO，懒签规避工具结果
持久化后预签名 URL 过期（1h）。
"""
from __future__ import annotations

import logging
import uuid

from fastmcp import FastMCP

from app.mcps.bind import mcps_state
from app.mcps.gd_ctx import GdCtx
from app.models import db as dbmod

logger = logging.getLogger(__name__)

# 面向 LLM 的 top-k 默认（schema 注册期固化值，不随运行配置漂移）
_KB_DEFAULT_K = 5

# hits 投影白名单（旧网关形态 + heading_path 供 LLM 引用结构位置）
_HIT_FIELDS = (
    "content", "score", "filename", "doc_id", "page", "chunk_index",
    "kb_id", "heading_path",
)


def _ok(query: str, hits: list[dict], message: str | None = None) -> dict:
    out = {"result": "ok", "query": query, "total": len(hits), "hits": hits}
    if message:
        out["message"] = message
    return out


def _err(message: str) -> dict:
    return {"result": "error", "message": message}


def register(mcp: FastMCP) -> None:
    """把知识库检索工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def kb_search(
        query: str,
        kb_ids: list[str],
        k: int = _KB_DEFAULT_K,
        _gd_ctx: GdCtx | None = None,
    ) -> dict:
        """检索本智能体绑定的知识库，返回 top-k 最相关文本片段。

        回答与知识库内容相关的问题前请先调用本工具检索，优先依据检索结果
        作答并标注来源；检索未命中或无相关内容时如实说明，再依据自身能力
        作答，不得编造检索结果。

        Args:
            query: 检索词（用户问题或其改写；可从多角度分次检索）
            kb_ids: 知识库 id 列表（必须使用工具描述中列出的真实 kb_id，禁止编造）
            k: 返回的 top-k 片段数（默认 5）
        """
        if not query.strip():
            return _err("检索词为空")
        if not kb_ids:
            return _err("未指定知识库（kb_ids 为空）")

        st = mcps_state()
        if st.knowledge_service is None:
            return _err("知识库能力未启用（kb_enabled=false）")

        # 身份校验：agent_id 现查 agent 行，kb_ids 必须落在其知识集内
        if _gd_ctx is None or not _gd_ctx.agent_id:
            return _err("缺少平台身份（_gd_ctx），无法校验知识库权限")
        try:
            agent_uuid = uuid.UUID(_gd_ctx.agent_id)
        except ValueError:
            return _err("平台身份非法（agent_id 不是有效 id）")
        agent_row = await dbmod.get_agent(st.pool, agent_uuid)
        if agent_row is None:
            return _err(f"智能体 {_gd_ctx.agent_id} 不存在（已删除？）")

        from app.core.schema_utils import AgentConfig

        try:
            allowed = (AgentConfig.model_validate(agent_row["config_json"]).kb_ids) or []
        except Exception:  # noqa: BLE001 — 配置损坏按不可校验拒绝（不崩溃工具调用）
            logger.warning("kb_search: agent %s config_json 损坏", _gd_ctx.agent_id)
            return _err(f"智能体 {_gd_ctx.agent_id} 配置损坏，无法校验知识库权限")
        invalid = [kid for kid in kb_ids if kid not in allowed]
        if invalid:
            return _err(
                f"kb_ids 不在本智能体的知识集内（疑似编造：{invalid}）；"
                f"可用知识库：{allowed or '（未绑定任何知识库）'}"
            )

        try:
            kids = [uuid.UUID(kid) for kid in kb_ids]
        except ValueError:
            return _err("kb_ids 含非法 id（不是有效 UUID）")

        try:
            results = await st.knowledge_service.search(kids, query, k=k)
        except Exception as e:  # noqa: BLE001 — 存储不可达/维度漂移等真实故障
            logger.warning("kb_search failed kb_ids=%s: %s", kb_ids, e)
            return _err(f"知识库检索失败: {e}")

        hits = [{f: r.get(f) for f in _HIT_FIELDS} for r in results]
        if not hits:
            return _ok(query, [], message="未检索到相关内容，请如实告知用户并依据自身能力作答")
        logger.info(
            "kb_search ok agent=%s kb_ids=%s hits=%d query=%s",
            _gd_ctx.agent_id, kb_ids, len(hits), query[:80],
        )
        return _ok(query, hits)
