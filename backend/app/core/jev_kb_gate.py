"""Jev KB 检索门控中间件（plan: jev-kb-rerank-middleware）。

形态 F2：middleware 包裹工具结果——`{server}__kb_search` 返回的 chunks 在
写入消息史之前经 Jev 相关性判决做"高置信无关才滤"的保守过滤；LLM 与
checkpoint 看到的都是滤后版本（shadow 只判不滤 / annotate 只标注不删）。

核心契约（与 shell 守卫 / rubric 门控同纪律）：

- 名字分发：非 `__kb_search` 后缀工具零介入（无 try、无判决、无改写，行为
  与中间件不存在时逐字节一致；可观测后果 = 一次字符串后缀判断）；
- handler 调用不进 try：工具异常/HITL 信号（GraphBubbleUp）原样上抛，由
  最外层 ToolErrorHandlingMiddleware 归一——"Jev 判决失败"绝不能被降格为
  "工具执行失败"（成功的检索结果被改写成 error 消息违反 fail-closed 语义）；
- process 永不抛出：门控自身任何异常兜底为 passthrough（原对象返回，
  identity 保持）——门控故障绝不杀死工具结果；
- fail-closed = passthrough = 现状行为：JevUnavailable（超时/HTTP 非 2xx/
  响应畸形）原样透传；k_boost 生效时按 ES 序截回原始 k（故障形态也不放大
  上下文）。unavailable 分支额外在 response_metadata 打 `jev_kb` 观测标记
  （UI 侧信道：SSE/历史序列化读取显示「jev 未介入」；该字段不进模型请求，
  LLM 可见形态与 content 逐字节不变）；
- 不对称 drop 规则：举证责任在"滤"侧——rel/info 两题同时高置信为否才
  drop，任一维度存疑（p 中间/conf 低）一律保留（歧义 = 维持现状）；
- keep_floor 兜底：ES 序前 N 条无论判决如何永不 drop（只修剪尾部，不斩首），
  输出集合永不为空（k ≥ 1 时 ≥ 1 条）；
- 每判必审计（jev_decision 单一判决台账，decision_type='kb_rerank'，零
  DDL 双用）；名字分发边界退出 / error 结果 / parse 失败不落表（未进入
  判决逻辑，无判决可记）；
- state/问题集以代码常量声明（非配置式，shell 守卫 QUESTIONS 同模型）：
  问题措辞 = 标定产物 = 行为契约，迁移/重构逐字保留，评审防措辞漂移。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_typesafe import Choice
from langgraph.errors import GraphBubbleUp

from app.services.jev_client import JevClient, JevUnavailable

logger = logging.getLogger(__name__)

KB_TOOL_SUFFIX = "__kb_search"
QUERY_AUDIT_MAX_CHARS = 400  # command 列承载 query 的截断上限
_CACHE_MAX_ENTRIES = 256  # 判决缓存上限（防长驻图无界增长）
# 送判 chunk 数封顶（plan §二：2×k ≤ 48 道题/次调用）。超出部分不送判也不
# drop（无判决 = 保留，保守侧），不截断 state。
KB_JUDGE_MAX_CHUNKS = 24


def is_kb_tool(name: str) -> bool:
    """名字分发守卫：仅 `{server}__kb_search` 后缀工具进入门控路径。"""
    return name.endswith(KB_TOOL_SUFFIX)


# ---------------------------------------------------------------------------
# 问题模板——定标产物，评审防措辞漂移
# ---------------------------------------------------------------------------
# 绑定机制：qid 不上送模型（wire 契约，jev_client.py），问题与具体 chunk 的
# 绑定必须写在 instructions 文本里——以反引号 state 路径显式引用（`query` /
# `chunks[i].text` / `chunks[i].source`，官方 skill 指南形态）。shell/rubric
# 均为整卷判决无需绑定，本出口是首个逐条判决出口。
# 形态：两选项 choice 中性键 yes/no（laya #156 官方缓解——noul 长文概率
# 饱和无区分度；choice 概率由 API 原生返回，无需本地推导）。措辞取原
# NoulCriteria true/false 原文，替换式改写、不并存双形态。
# 极性登记（沿用 jev_gate.py 注释纪律）：
# - c{i}__rel / c{i}__info：yes = 保留侧（相关 / 含具体信息）；
# - drop 条件在两题同时高置信选 no 侧才成立（反向合成，见 ChunkJudgment.drop）。
KB_Q_REL = Choice(
    instructions=(
        "查询 `query` 与片段 `chunks[{i}]`（`chunks[{i}].text`，来源 "
        "`chunks[{i}].source`）讨论的是同一对象或同一主题"
    ),
    criteria={
        "yes": "片段内容针对查询所问的具体对象或主题，或直接提供其相关信息",
        "no": "片段仅与查询同属一个更大类别或领域（如同品牌的其它型号、"
        "同行业的其它公司、同一文档库的其它主题），或与查询主题无关",
    },
)
KB_Q_INFO = Choice(
    instructions="片段 `chunks[{i}].text` 包含具体、可用的信息内容",
    criteria={
        "yes": "事实、数据、定义、论证、操作步骤，或带实质细节的描述",
        "no": "页眉页脚页码、目录条目、孤立标题行、版权或法律声明、"
        "重复的导航文字、表格格式残片、OCR 乱码、纯占位符",
    },
)


def _render_source(hit: dict) -> str:
    """hit → 人类可读来源串（state schema 的 source 字段）。"""
    source = str(hit.get("filename") or "unknown")
    if hit.get("page") is not None:
        source += f" p.{hit['page']}"
    if hit.get("chunk_index") is not None:
        source += f"|chunk #{hit['chunk_index']}"
    return source


def render_state(query: str, hits: list[dict]) -> str:
    """构建送判 state（命名 JSON 字段，schema 见 plan §二）。

    刻意排除的字段（判定无关或有害）：kb_ids（库身份不改变相关性）；ES
    cosine score（嵌入相似度的二次意见是本门控的存在理由，送入会锚定/偏置
    独立判断）。state 不截断：chunk 已被 ingest 期 chunk_size 天然有界；
    超长由 API 拒绝 → JevUnavailable → 透传（宁可不判，不发残卷）。
    """
    payload = {
        "query": query,
        "chunks": [
            {
                "i": i,
                "text": hit.get("content") or "",
                "source": _render_source(hit),
            }
            for i, hit in enumerate(hits)
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def render_questions(num_chunks: int) -> dict[str, Choice]:
    """按 chunk 数实例化问题集（qid：c{i}__rel / c{i}__info，i 为 ES 序下标）。

    qid 只在单次判决内使用、不跨调用持久化（与 rubric c{i} 同语义）。
    criteria 复用模板常量对象（不可变值对象，无共享状态风险）。
    """
    questions: dict[str, Choice] = {}
    for i in range(num_chunks):
        questions[f"c{i}__rel"] = Choice(
            instructions=KB_Q_REL.instructions.format(i=i),
            criteria=KB_Q_REL.criteria,
        )
        questions[f"c{i}__info"] = Choice(
            instructions=KB_Q_INFO.instructions.format(i=i),
            criteria=KB_Q_INFO.criteria,
        )
    return questions


# ---------------------------------------------------------------------------
# 结果解析（plan §九.1：严格解析，拒绝宽松匹配）
# ---------------------------------------------------------------------------


def _extract_text(content: Any) -> str | None:
    """ToolMessage.content → JSON 文本；非文本形态返回 None。

    接受两种规范形态：裸字符串；单 text 块列表（FastMCP dict 返回经
    langchain-mcp-adapters 的实际 wire 形态）。其余（多块/图像/非文本）
    一律 None（passthrough，不产残卷）。
    """
    if isinstance(content, str):
        return content
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
    ):
        return content[0]["text"]
    return None


def parse_kb_result(content: Any) -> dict | None:
    """解析 kb_search 返回 → 校验通过的 payload dict（含 hits）；失败 None。

    与 mcp-servers kb_search 的返回契约对齐：{"result": "ok", "query",
    "total", "hits": [{content, score, filename, doc_id, page, chunk_index,
    kb_id}]}。严格校验：result=="ok" ∧ hits 为 list ∧ 每项为 dict 且
    content 字段为 str。error 形态/自由文本/字段漂移 → None（调用方
    passthrough；不做宽松解析——脆弱，拒绝）。
    """
    text = _extract_text(content)
    if text is None:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("result") != "ok":
        return None
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return None
    for hit in hits:
        if not isinstance(hit, dict) or not isinstance(hit.get("content"), str):
            return None
    return payload


# ---------------------------------------------------------------------------
# 判决合成（plan §五：本地布尔合成，无二次网络调用）
# ---------------------------------------------------------------------------


@dataclass
class ChunkJudgment:
    """单 chunk 的 rel/info 双题判决（P = choice yes 侧概率，conf 为 API 原生值）。"""

    rel_p: float
    rel_conf: float
    info_p: float
    info_conf: float

    def drop(self, drop_p: float, drop_conf: float) -> bool:
        """不对称 drop 规则（举证责任在"滤"侧）：

        两题同时高置信为 no 侧才 drop（p(yes) ≤ drop_p ∧ conf ≥ drop_conf
        双条且）；任一维度存疑（p 中间 / conf 低）→ 保留（歧义 = 维持现状）。
        """
        return (
            self.rel_p <= drop_p
            and self.rel_conf >= drop_conf
            and self.info_p <= drop_p
            and self.info_conf >= drop_conf
        )

    def describe(self) -> str:
        """概率摘要（annotate 前缀 / all_low footer / 审计日志共用）。"""
        return f"rel={self.rel_p:.2f}/info={self.info_p:.2f}"


def _chunk_answer(answers: dict, qid: str) -> tuple[float, float]:
    ans = answers.get(qid)
    if not isinstance(ans, dict) or ans.get("type") != "choice":
        raise ValueError(f"答案缺失或畸形: {qid}")
    probs = ans.get("probabilities") or {}
    if "yes" not in probs:
        raise ValueError(f"答案缺少 yes 概率: {qid}")
    return float(probs["yes"]), float(ans.get("confidence", 0.0))


def synthesize_kb(answers: dict, num_chunks: int) -> list[ChunkJudgment]:
    """JevVerdict.answers → 逐 chunk 双题判决。

    答案覆盖全部 2×k 道题由 client.normalize_answers 契约保证；此处对
    畸形答案抛 ValueError（调用方归一为不可用语义 → 透传，fail-closed）。
    """
    judgments: list[ChunkJudgment] = []
    for i in range(num_chunks):
        rel_p, rel_conf = _chunk_answer(answers, f"c{i}__rel")
        info_p, info_conf = _chunk_answer(answers, f"c{i}__info")
        judgments.append(
            ChunkJudgment(rel_p=rel_p, rel_conf=rel_conf, info_p=info_p, info_conf=info_conf)
        )
    return judgments


def _cache_key(query: str, hits: list[dict]) -> str:
    """判决缓存键：sha256(query, 逐 chunk (doc_id, chunk_index, content_hash))。

    content 入键防 reindex 后拿陈旧判决（两出口同纪律：同键只判一次）。
    键对 chunk 序列敏感（ES 序即送判序）。
    """
    digest = hashlib.sha256()
    digest.update(query.encode("utf-8"))
    for hit in hits:
        digest.update(str(hit.get("doc_id") or "").encode("utf-8"))
        digest.update(str(hit.get("chunk_index") or "").encode("utf-8"))
        digest.update(hashlib.sha256((hit.get("content") or "").encode("utf-8")).digest())
    return digest.hexdigest()


def _thread_id(request: Any) -> str:
    """thread_id 提取（审计关联用，缺省空串；plan §九.2）。

    优先 langgraph.config.get_config()（官方钦定形态，awrap_tool_call 运行
    在图任务上下文内）；不可得时回落 request.runtime.config（ToolRuntime
    子类持有 config，shell 守卫同款）。
    """
    tid = ""
    try:
        from langgraph.config import get_config

        configurable = ((get_config() or {}).get("configurable") or {})
        tid = str(configurable.get("thread_id") or "")
    except Exception:  # noqa: BLE001 — 无图上下文（单测/离线）属正常
        tid = ""
    if tid:
        return tid
    runtime = getattr(request, "runtime", None)
    configurable = ((getattr(runtime, "config", None) or {}).get("configurable") or {})
    return str(configurable.get("thread_id") or "")


# ---------------------------------------------------------------------------
# 判决器 + 中间件
# ---------------------------------------------------------------------------


class KbRerankGate:
    """kb 检索门控判决器：parse → 判决（缓存）→ mode 分支 → 改写/透传。

    process 永不抛出（RubricJudge 同纪律）：任何意外异常兜底为原对象返回。
    passthrough 一律返回原对象（identity 保持）——唯一例外是 unavailable 分支：
    返回浅拷贝并打 response_metadata['jev_kb'] 观测标记（content 等字段原引用，
    LLM 可见形态不变，见 _stamp_jev_unavailable）；改写一律构造新 ToolMessage
    （tool_call_id/name 永不改）。阈值随图构建烘焙（TTL 语义与两出口一致）。

    审计边界：进入判决路径的调用一行（parse ok 即入：filtered / annotated /
    all_low / passthrough / unavailable）；缓存命中不重复落表；error 结果 /
    parse 失败 / 名字分发退出不落表。判决缓存只存成功判决——unavailable 无
    判决数据可复用，不缓存以便即时恢复（fail-closed 语义不受影响）。
    """

    def __init__(
        self,
        client: JevClient,
        audit_submit: Callable[[dict], None] | None,
        agent_id: uuid.UUID,
        *,
        mode: str = "shadow",
        drop_p: float = 0.15,
        drop_conf: float = 0.90,
        keep_floor: int = 2,
        k_boost: int = 0,
        state_max_chars: int = 12000,
    ):
        self._client = client
        self._audit_submit = audit_submit
        self._agent_id = agent_id
        self._mode = mode
        self._drop_p = drop_p
        self._drop_conf = drop_conf
        self._keep_floor = keep_floor
        self._k_boost = k_boost
        self._state_max_chars = state_max_chars
        self._cache: OrderedDict[str, tuple[list[ChunkJudgment], dict, dict, int, str]] = (
            OrderedDict()
        )
        self._cache_lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    # ---------------- 缓存（同键只判一次，两出口同纪律）----------------
    # 值为 (judgments, answers, raw, latency_ms, model)：answers/raw 随判决
    # 保存，供审计行携带完整留痕（缓存命中不落表，故只有 miss 路径消费）。
    def _cache_get(self, key: str) -> tuple[list[ChunkJudgment], dict, dict, int, str] | None:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_put(
        self, key: str, entry: tuple[list[ChunkJudgment], dict, dict, int, str]
    ) -> None:
        with self._cache_lock:
            self._cache[key] = entry
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    # ---------------- k_boost 预包裹（两段式检索，默认关）----------------
    def pre_wrap(self, request: Any) -> tuple[Any, int | None]:
        """k_boost>0 且 args.k 存在时改写 k（过采）；返回 (request, k0)。

        不可变形态：经 request.override 构造新请求（原 tool_call dict 同时挂
        在 AIMessage.tool_calls 上，就地突变会污染消息史）。k0=None 表示未
        改写（后续无需截回）。
        """
        if self._k_boost <= 0:
            return request, None
        tool_call = getattr(request, "tool_call", None) or {}
        args = tool_call.get("args") or {}
        if "k" not in args:
            return request, None
        try:
            k0 = int(args["k"])
        except (TypeError, ValueError):
            return request, None
        new_call = {**tool_call, "args": {**args, "k": self._k_boost}}
        return request.override(tool_call=new_call), k0

    # ---------------- 判决不可用统一出口（audit + 观测标记 + 透传/k0 截回）----------------
    def _unavailable_outcome(
        self,
        result: Any,
        payload: dict,
        hits: list[dict],
        k0: int | None,
        *,
        query: str,
        tool_call_id: str,
        thread_id: str,
        state_text: str | None = None,
        annotation: str = "",
    ) -> Any:
        logger.warning("jev kb judge unavailable (fail-closed → passthrough): %s", annotation)
        self._audit(
            source="unavailable",
            verdict="unavailable",
            query=query,
            tool_call_id=tool_call_id,
            thread_id=thread_id,
            state_text=state_text,
            annotation=annotation or None,
        )
        if k0 is not None and len(hits) > k0:
            return _stamp_jev_unavailable(self._rewrite(result, payload, hits[:k0]))
        return _stamp_jev_unavailable(result)

    # ---------------- 主入口（永不抛出）----------------
    async def process(self, request: Any, result: Any, k0: int | None = None) -> Any:
        try:
            return await self._process(request, result, k0)
        except GraphBubbleUp:
            raise  # HITL 信号纪律：直通，绝不吞（process 内无此来源，防御性）
        except Exception:
            logger.exception("jev kb gate internal error (fail-closed → passthrough)")
            return result

    async def _process(self, request: Any, result: Any, k0: int | None) -> Any:
        # 1. 非 ToolMessage / error 结果 / 非文本 content → 原对象返回（不落表）
        if (
            not isinstance(result, ToolMessage)
            or result.status == "error"
            or _extract_text(result.content) is None
        ):
            return result
        tool_call = getattr(request, "tool_call", None) or {}
        tool_call_id = str(tool_call.get("id") or "")
        query = str((tool_call.get("args") or {}).get("query") or "")
        # 2. parse 失败 → 原对象返回（格式漂移，不落表——未进入判决逻辑）
        payload = parse_kb_result(result.content)
        if payload is None:
            logger.info(
                "jev kb gate: unparsed kb_search content (passthrough) tc=%s", tool_call_id
            )
            return result
        hits: list[dict] = payload["hits"]
        # 3. keep_floor 余量判定：chunks ≤ keep_floor+1 无可滤余量 → 透传；
        #    rule 分支落表供灰度对账（shell 守卫白名单同式）
        if len(hits) <= self._keep_floor + 1:
            self._audit(
                source="rule_keep_floor",
                verdict="passthrough",
                query=query,
                tool_call_id=tool_call_id,
                thread_id=_thread_id(request),
            )
            return result
        # 4. 判决（缓存查询 → 命中或新判）；只判前 KB_JUDGE_MAX_CHUNKS 条。
        #    state 超长本地拦截（宁拒不发残卷，对齐 rubric 纪律）：超限不送判，
        #    直接按 unavailable 透传——不缓存，超长诱因消失后即时恢复。
        judged = hits[:KB_JUDGE_MAX_CHUNKS]
        state_text = render_state(query, judged)
        if len(state_text) > self._state_max_chars:
            return self._unavailable_outcome(
                result,
                payload,
                hits,
                k0,
                query=query,
                tool_call_id=tool_call_id,
                thread_id=_thread_id(request),
                annotation=(
                    f"state {len(state_text)} chars > jev_kb_state_max_chars"
                    f"({self._state_max_chars})，本地拦截不送判"
                ),
            )
        key = _cache_key(query, judged)
        thread_id = _thread_id(request)
        cached = self._cache_get(key)
        from_cache = cached is not None

        def audit_judged(verdict_name: str) -> None:
            # 缓存命中不重复落表（同一判决已在台账；RubricJudge 同先例）
            if from_cache:
                return
            self._audit(
                source="jev",
                verdict=verdict_name,
                query=query,
                tool_call_id=tool_call_id,
                thread_id=thread_id,
                state_text=state_text,
                answers=answers,
                raw=raw,
                latency_ms=latency_ms,
                model=model,
            )

        if cached is not None:
            judgments, answers, raw, latency_ms, model = cached
        else:
            try:
                verdict = await self._client.ajudge(
                    state_text, render_questions(len(judged))
                )
                judgments = synthesize_kb(verdict.answers, len(judged))
                answers, raw = verdict.answers, verdict.raw
                latency_ms, model = verdict.latency_ms, verdict.model
            except (JevUnavailable, ValueError) as e:
                # 判决不可用（含畸形答案）→ 透传；k_boost 生效时按 ES 序截回
                # k0——不可用时上下文形态也必须等于现状。
                return self._unavailable_outcome(
                    result,
                    payload,
                    hits,
                    k0,
                    query=query,
                    tool_call_id=tool_call_id,
                    thread_id=thread_id,
                    state_text=state_text,
                    annotation=str(e),
                )
            self._cache_put(key, (judgments, answers, raw, latency_ms, model))
        # 5. 本地合成 → 三态出口
        raw_drops = [j.drop(self._drop_p, self._drop_conf) for j in judgments]
        eff_drops = list(raw_drops)
        for i in range(min(self._keep_floor, len(eff_drops))):
            eff_drops[i] = False  # floor 豁免：ES 序前 N 条永不 drop
        # 未送判的尾部（> KB_JUDGE_MAX_CHUNKS）一律保留：无判决 = 不 drop（保守侧）
        eff_drops.extend([False] * (len(hits) - len(eff_drops)))
        all_low = all(raw_drops)
        dropped = sum(eff_drops)

        if self._mode == "annotate":
            audit_judged("annotated")
            return self._rewrite_annotated(result, payload, hits, judgments)

        if self._mode == "shadow":
            # 校准态：不改写 content，审计记录"会滤什么"（as-if enforce）
            audit_judged("all_low" if all_low else ("filtered" if dropped else "passthrough"))
            return result

        # enforce
        if not dropped:
            audit_judged("passthrough")
            return result
        if all_low:
            # 判决层面全灭：输出仍为 floor 豁免的保底条目 + 全低警示（透明 +
            # 恢复路径可及；floor 保内容，警示保不误导）
            per_chunk = "；".join(f"c{i} {j.describe()}" for i, j in enumerate(judgments))
            note = (
                f"（检索返回 {len(judged)} 条，相关性均低：{per_chunk}——"
                "以上保留条目仅为保底，可信度低；建议换述查询重试。）"
            )
            survivors = [h for h, d in zip(hits, eff_drops) if not d]
            audit_judged("all_low")
            return self._rewrite(result, payload, survivors, note=note)
        note = f"（相关性门控：检索返回 {len(hits)} 条，已滤除 {dropped} 条高置信无关片段）"
        survivors = [h for h, d in zip(hits, eff_drops) if not d]
        audit_judged("filtered")
        return self._rewrite(result, payload, survivors, note=note)

    # ---------------- 改写（新 ToolMessage；tool_call_id/name 永不改）----------------
    def _rewrite(
        self,
        result: ToolMessage,
        payload: dict,
        hits: list[dict],
        note: str | None = None,
        annotations: dict[int, str] | None = None,
    ) -> ToolMessage:
        out = dict(payload)
        out_hits = []
        for i, hit in enumerate(hits):
            new_hit = dict(hit)
            if annotations is not None and i in annotations:
                new_hit["relevance"] = annotations[i]
            out_hits.append(new_hit)
        out["hits"] = out_hits
        out["total"] = len(out_hits)
        if note:
            out["note"] = note
        text = json.dumps(out, ensure_ascii=False)
        return ToolMessage(
            content=_mirror_content(result.content, text),
            tool_call_id=result.tool_call_id,
            name=getattr(result, "name", None) or None,
            status=getattr(result, "status", None) or None,
        )

    def _rewrite_annotated(
        self, result: ToolMessage, payload: dict, hits: list[dict], judgments: list[ChunkJudgment]
    ) -> ToolMessage:
        annotations = {
            i: f"[相关性 {j.describe()}]" for i, j in enumerate(judgments)
        }
        return self._rewrite(result, payload, hits, annotations=annotations)

    # ---------------- 审计 ----------------
    def _audit(
        self,
        *,
        source: str,
        verdict: str,
        query: str,
        tool_call_id: str,
        thread_id: str,
        state_text: str | None = None,
        answers: dict | None = None,
        raw: dict | None = None,
        latency_ms: int | None = None,
        model: str | None = None,
        annotation: str | None = None,
    ) -> None:
        record = {
            "decision_type": "kb_rerank",
            "session_id": None,  # 会话行与 thread 的映射在业务层，门控层只有 thread_id
            "thread_id": thread_id or "unknown",
            "agent_id": self._agent_id,
            "tool_call_id": tool_call_id or "unknown",  # 真实 id（优于 rubric 合成键）
            "mode": self._mode,
            "source": source,
            "verdict": verdict,
            # command 列承载 query（截断 400 字符；列已放开 NOT NULL）
            "command": query[:QUERY_AUDIT_MAX_CHARS] or None,
            "state_rendered": state_text,
            "answers": ({**answers, "_raw": raw} if answers is not None else None),
            "latency_ms": latency_ms,
            "model": model,
        }
        logger.info(
            "jev kb gate | agent=%s verdict=%s source=%s mode=%s%s tc=%s query=%r",
            self._agent_id,
            verdict,
            source,
            self._mode,
            f" | {annotation}" if annotation else "",
            tool_call_id,
            query[:120],
        )
        if self._audit_submit is not None:
            self._audit_submit(record)


def _mirror_content(original: Any, new_text: str) -> Any:
    """按原容器形态回写（str → str；单 text 块列表 → 同形态新列表）。

    parse 已保证 content 属这两种形态之一；此处镜像保持下游消息形态一致。
    """
    if isinstance(original, str):
        return new_text
    return [{**original[0], "text": new_text}]


def _stamp_jev_unavailable(result: ToolMessage) -> ToolMessage:
    """unavailable 透传的观测标记：浅拷贝 + response_metadata['jev_kb']。

    model_copy 浅拷贝——content/status/tool_call_id/name/artifact 全部原引用
    保持，LLM 可见形态逐字节等于现状（response_metadata 不进模型请求）；
    仅 SSE（stream_adapter tool_result 载荷）与历史（_serialize_message）
    读取该键，前端显示「jev 未介入（判决不可用）」徽标。观测增强非行为
    变更：失败语义仍是 fail-closed 透传，不产生 error、不改写 hits。
    """
    meta = dict(getattr(result, "response_metadata", None) or {})
    meta["jev_kb"] = "unavailable"
    return result.model_copy(update={"response_metadata": meta})


class JevKbRerankMiddleware(AgentMiddleware):
    """kb 检索门控中间件：名字分发守卫 + 判决器属性注册。

    判决发生在 handler（真实 kb_search）执行完成之后、ToolMessage 写入
    messages 通道之前（形态 F2）。非 kb 工具零介入：一次后缀判断后原样
    委派，无 try（异常语义 = 无此中间件）。置于 ToolErrorHandlingMiddleware
    内侧装配（first defined = outermost，error 归一化在最外层兜底全部工具）。
    """

    def __init__(self, kb_judge: KbRerankGate, **kwargs):
        super().__init__(**kwargs)
        self._kb_judge = kb_judge  # judge 属性注册（_JevRubricBase._jev_judge 同式）

    @property
    def name(self) -> str:
        return "JevKbRerankMiddleware"

    @property
    def kb_mode(self) -> str:
        """运行态（shadow|annotate|enforce）——挂载期日志可见性用。"""
        return self._kb_judge.mode

    async def awrap_tool_call(self, request, handler):
        tool_call = getattr(request, "tool_call", None) or {}
        if not is_kb_tool(str(tool_call.get("name") or "")):
            return await handler(request)  # 零介入：无 try，行为逐字节一致
        request, k0 = self._kb_judge.pre_wrap(request)
        result = await handler(request)  # 不进 try：工具异常/GraphBubbleUp 原样
        # 上抛，由最外层 error 中间件归一（两层职责不混）
        return await self._kb_judge.process(request, result, k0)


def build_jev_kb_gate(
    settings: Any,
    audit_submit: Callable[[dict], None] | None,
    agent_id: uuid.UUID,
) -> JevKbRerankMiddleware:
    """构建 kb 门控中间件（与 build_shell_guard / build_rubric_judge 平行的出口）。

    Args:
        settings: Settings（mode/阈值/k_boost 随构建烘焙，改配置需重启后端
            或等 registry TTL 过期——与两出口同语义）。kb 门控使用独立超时
            预算（jev_kb_timeout_seconds，本地部署 RTT 小）。
        audit_submit: 审计回调（dict → None）；None 时只打日志不落库。
        agent_id: 构建期烘焙（审计表 NOT NULL 列）。
    """
    gate = KbRerankGate(
        JevClient(
            base_url=settings.jev_base_url,
            api_key=settings.typesafe_api_key,
            model=settings.jev_model,
            timeout_s=settings.jev_kb_timeout_seconds,
            proxy_url=getattr(settings, "jev_proxy_url", ""),
        ),
        audit_submit,
        agent_id,
        mode=settings.jev_kb_mode,
        drop_p=float(settings.jev_kb_drop_probability),
        drop_conf=float(settings.jev_kb_drop_confidence),
        keep_floor=int(settings.jev_kb_keep_floor),
        k_boost=int(settings.jev_kb_k_boost),
        state_max_chars=int(getattr(settings, "jev_kb_state_max_chars", 12000)),
    )
    return JevKbRerankMiddleware(gate)
