"""Jev (TypeSafe System One) client + shell-guard audit sink.

传输层基于 LangChain 官方集成 langchain-typesafe（`TypeSafeClassifier`，
Runnable invoke/ainvoke），本模块是其门面：保留 `judge(state) → JevVerdict`
契约与 fail-closed 语义，上层（jev_gate / 审计 / 测试）零改动。

官方库与手写客户端的三处差异，均由本门面吸收：
- 响应经 pydantic 校验为类型化对象（NoulAnswer/ChoiceAnswer，吸收手写 normalize
  的大部分防御面）；但"answers 须覆盖全部 questions"与"choice 概率 ∈ [0,1]"
  两契约库不约束 → normalize_answers 补齐（fail-closed 不降级）。
- Noul 答案不携带 confidence（仅 P(yes)=noul）→ 本地推导 |2p-1|——恰为 API 自身
  的二元 choice 置信度公式 ((2*peak-1)/(n-1), n=2)，语义一致。
- 异常体系为 TypeSafeError 层级 → 统一映射 JevUnavailable；分类器懒构造，
  构造失败（如配置非法）也按"当次不可用"处理，绝不让门控炸掉图构建。

问题集参数化（rubric 门控复用同一 client）：
- `judge/ajudge/normalize_answers` 均接受 `questions=None`（缺省=模块级
  QUESTIONS，shell 守卫行为逐字节不变）；rubric 门控传入按节点配置渲染的
  问题集。同步路径走 invoke（when() 契约所迫）；异步路径走 aainvoke→ainvoke，
  供 rubric 的 `_ainvoke_grader` 覆写使用（不阻塞事件循环）。

Wire contract（https://docs.typesafe.ai/api，与手写时代逐字段一致）：
    POST {base}/v1/systemone
    Authorization: Bearer <key>
    { "model": "jev-1.13.0", "state": "...", "questions": {qid: {type, instructions, criteria?}} }
    → { "model": "...", "answers": {...}, "usage": {...} }

其余不变的事实：
- shell 门控在 after_model 内同步执行，故走 invoke（同步）+ 硬超时；无重试——
  一次未命中即 fail-closed 问人（plan R1/R5）。
- 每次调用（成功或失败）产出一条审计记录；异步 DB 写经 `JevAuditSink`
  （有界队列 + drainer 任务）解耦。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from langchain_typesafe import (
    Choice,
    ChoiceAnswer,
    ClassifierRequest,
    ClassifierResponse,
    Noul,
    NoulAnswer,
    Question,
    TypeSafeClassifier,
)
from langchain_typesafe.client import TypeSafeError
from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod

logger = logging.getLogger(__name__)


class JevUnavailable(Exception):
    """Jev 判决不可得（超时 / HTTP 非 2xx / 响应解析失败 / 未配置）。

    门控侧一律按"问人"处理（fail-closed）。绝不存在 fail-open 分支。
    """


# ---------------------------------------------------------------------------
# Question set (plan §4.1) — 一次并行问全，加维度零成本
# ---------------------------------------------------------------------------
# 极性登记（plan R6，防措辞方向与阈值方向错配）：
# - 正向题（高 P = 安全）：scope_routine
# - 反向题（高 P = 危险）：touches_system / installs_packages
# - 观察维度（不参与合成，仅落审计）：is_reversible / touches_net
# - 选项题：risk_tier（probabilities["safe"] 为安全侧）
# 措辞与 criteria 为标定产物，属行为契约——迁移传输层时逐字保留。
QUESTIONS: dict[str, Question] = {
    "scope_routine": Noul(
        instructions="该命令属于常规开发操作——读写当前工作区内文件、创建目录、"
        "运行脚本/构建/测试、查询状态；不修改/删除系统文件、全局配置、注册表、"
        "系统服务、计划任务、启动项、凭证文件（.ssh/authorized_keys、.bashrc 等）"
        "或其他用户目录下的数据",
    ),
    "touches_system": Noul(
        instructions="该命令修改/删除系统文件、全局配置、注册表、系统服务、"
        "计划任务、启动项、凭证文件或其他用户目录下的数据",
    ),
    "installs_packages": Noul(
        instructions="该命令安装或更新软件包（pip/npm/uv/apt/choco/winget 等包管理器）",
    ),
    "is_reversible": Noul(
        instructions="判断错误后分钟级可恢复（git/备份/重装）",
    ),
    "touches_net": Noul(
        instructions="发起网络连接、下载或外传数据；包管理器安装由 installs_packages 单独刻画，此处不含",
    ),
    "risk_tier": Choice(
        instructions="按最坏子命令评估；管道/链式命令取风险最高一段",
        criteria={
            "safe": "无副作用或仅影响当前工作区内的常规开发操作",
            "low": "有副作用但局限于项目目录，或分钟级可恢复",
            "high": "影响系统配置/全局状态/其他用户数据，或破坏性不可逆",
        },
    ),
}


@dataclass
class JevVerdict:
    """一次 systemone 调用的归一化判决。

    `answers` 的每项为 {type, ...原始字段..., "confidence": float}——noul 的
    confidence 为本地推导值 |2p-1|（见模块 docstring），choice 为 API 返回值。
    answers/raw 为 JSON-ready 纯 dict（审计 Json() 落库可直接序列化）。
    """

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # API 原始 answers + usage（审计留痕）
    latency_ms: int = 0
    model: str = ""


def _number(value: Any) -> float:
    n = float(value)
    if not 0.0 <= n <= 1.0:
        raise JevUnavailable(f"概率/置信度越界 [0,1]: {value!r}")
    return n


def normalize_answers(
    response: ClassifierResponse,
    questions: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """把 ClassifierResponse 的 answers 归一为逐题 {type, ..., confidence}。

    官方库的 pydantic 校验已吸收大部分畸形防御面；本函数补齐库不约束的
    三个契约：答案覆盖全部问题、答案 type 与问题 type 匹配、choice 概率
    ∈ [0,1]（probabilities 字段在库侧无数值界约束）。
    任何违反 → JevUnavailable（fail-closed）。

    Args:
        response: TypeSafeClassifier 的原始响应。
        questions: 参与本次判定的问题集；None=模块级 QUESTIONS（shell 守卫）。
            rubric 门控传入按节点渲染的问题集，答案覆盖按该集合校验。
    """
    qs = questions if questions is not None else QUESTIONS
    out: dict[str, dict[str, Any]] = {}
    for qid, question in qs.items():
        answer = response.answers.get(qid)
        if answer is None:
            raise JevUnavailable(f"答案缺失: {qid}")
        if isinstance(question, Noul):
            if not isinstance(answer, NoulAnswer):
                raise JevUnavailable(f"答案 type 不匹配: {qid} 期望 noul 实得 {answer.type!r}")
            p = _number(answer.noul)
            # 二元分布的 API 式置信度：(2*peak-1)/(2-1) == |2p-1|
            out[qid] = {"type": "noul", "noul": p, "confidence": abs(2.0 * p - 1.0)}
        elif isinstance(question, Choice):
            if not isinstance(answer, ChoiceAnswer):
                raise JevUnavailable(f"答案 type 不匹配: {qid} 期望 choice 实得 {answer.type!r}")
            out[qid] = {
                "type": "choice",
                "choice": answer.choice,
                "probabilities": {k: _number(v) for k, v in answer.probabilities.items()},
                "confidence": _number(answer.confidence),
            }
        else:  # pragma: no cover - 当前问题集不含 score
            raise JevUnavailable(f"未支持的问题类型: {qid}")
    return out


def _raw_payload(response: ClassifierResponse) -> dict[str, Any]:
    """响应原始 answers + usage 转 JSON-ready 纯值（审计留痕）。"""
    return {
        "answers": {qid: answer.model_dump(mode="json") for qid, answer in response.answers.items()},
        "usage": response.usage.model_dump(mode="json"),
    }


class JevClient:
    """`TypeSafeClassifier` 的同步门面（复用其连接池；httpx2.Client 线程安全）。

    分类器懒构造：官方库对非法配置（如空 key）在构造期即抛错，而门控配置项
    失误不应炸掉整图构建——懒构造把该失败映射为当次 JevUnavailable
    （问人回落），与 judge 路径的 fail-closed 语义一致。
    """

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_s: float):
        self._model = model
        self._classifier: TypeSafeClassifier | None = None
        self._classifier_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key,
            "base_url": base_url.rstrip("/"),
            "timeout": timeout_s,
        }

    @property
    def model(self) -> str:
        return self._model

    def _get_classifier(self) -> TypeSafeClassifier:
        if self._classifier is None:
            try:
                self._classifier = TypeSafeClassifier(**self._classifier_kwargs)
            except Exception as e:  # 构造失败=当次不可用，绝不炸图
                raise JevUnavailable(f"TypeSafe 客户端构建失败: {type(e).__name__}: {e}") from e
        return self._classifier

    def judge(self, state: str, questions: dict[str, Any] | None = None) -> JevVerdict:
        """一次并行求值全部问题（同步）；任何失败统一抛 JevUnavailable。

        Args:
            state: 送判状态文本。
            questions: 问题集；None=模块级 QUESTIONS（shell 守卫，行为不变）。
        """
        request: ClassifierRequest = {
            "state": state,
            "questions": questions if questions is not None else QUESTIONS,
        }
        started = time.perf_counter()
        try:
            response = self._get_classifier().invoke(request)
        except TypeSafeError as e:
            raise JevUnavailable(f"TypeSafe 调用失败: {e}") from e
        except Exception as e:  # fail-closed：意外异常同样归一为不可用
            raise JevUnavailable(f"TypeSafe 调用异常: {type(e).__name__}: {e}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)
        return JevVerdict(
            answers=normalize_answers(response, questions),
            raw=_raw_payload(response),
            latency_ms=latency_ms,
            model=response.model or self._model,
        )

    async def ajudge(self, state: str, questions: dict[str, Any] | None = None) -> JevVerdict:
        """`judge` 的异步镜像：走 classifier.ainvoke（httpx2.AsyncClient）。

        供 rubric 门控的 `_ainvoke_grader` 覆写使用——该方法运行在事件循环
        上，同步 invoke 会阻塞 loop 最长 timeout 秒。异常映射与 `judge`
        完全一致（任何失败统一 JevUnavailable，门控侧 fail-closed 委派）。
        """
        request: ClassifierRequest = {
            "state": state,
            "questions": questions if questions is not None else QUESTIONS,
        }
        started = time.perf_counter()
        try:
            response = await self._get_classifier().ainvoke(request)
        except TypeSafeError as e:
            raise JevUnavailable(f"TypeSafe 调用失败(异步): {e}") from e
        except Exception as e:  # fail-closed：意外异常同样归一为不可用
            raise JevUnavailable(f"TypeSafe 调用异常(异步): {type(e).__name__}: {e}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)
        return JevVerdict(
            answers=normalize_answers(response, questions),
            raw=_raw_payload(response),
            latency_ms=latency_ms,
            model=response.model or self._model,
        )

    def close(self) -> None:
        # 只关同步 client；async client 从未发请求、不持连接，无需清理。
        with contextlib.suppress(Exception):
            if self._classifier is not None and self._classifier.client is not None:
                self._classifier.client.close()


def _strip_nuls(value: Any) -> Any:
    """递归剔除字符串中的 NUL(0x00) 字节（PostgreSQL text 列拒收）。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _strip_nuls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_nuls(v) for v in value]
    return value


class JevAuditSink:
    """有界内存队列 + 异步 drainer：同步门控路径 → 异步 DB 写。

    `submit` 在 when() 内同步调用（事件循环线程），必须零阻塞：队满丢弃并告警
    （审计缺失可接受——门控本身 fail-closed，不依赖审计落地）。drainer 由
    main lifespan 启动/停止，单任务串行写库。

    入队前剔除文本中的 NUL(0x00)：PostgreSQL text 字段拒收 NUL，MCP 工具
    返回的转写常夹带 NUL——不清洗会导致整条审计记录 insert 失败被丢弃。
    """

    def __init__(self, max_pending: int = 2000):
        self._records: deque[dict] = deque()
        self._lock = threading.Lock()
        self._max_pending = max_pending
        self._dropped = 0
        self._pool: AsyncConnectionPool | None = None
        self._task: Any | None = None
        self._wakeup: Any | None = None

    @property
    def started(self) -> bool:
        return self._task is not None

    def submit(self, record: dict) -> None:
        """入队一条审计记录（非阻塞；满则丢弃计数）。"""
        record = _strip_nuls(record)
        with self._lock:
            if len(self._records) >= self._max_pending:
                self._dropped += 1
                if self._dropped % 100 == 1:  # 限频告警，防日志风暴
                    logger.warning("jev audit queue full; dropped %d record(s) so far", self._dropped)
                return
            self._records.append(record)
        if self._wakeup is not None:
            self._wakeup.set()

    def _take_batch(self, limit: int = 64) -> list[dict]:
        with self._lock:
            batch = []
            while self._records and len(batch) < limit:
                batch.append(self._records.popleft())
            return batch

    async def start(self, pool: AsyncConnectionPool) -> None:
        """启动 drainer（幂等；重复调用仅刷新 pool 引用）。"""
        import asyncio

        if self._task is not None:
            self._pool = pool
            return
        self._pool = pool
        self._wakeup = asyncio.Event()

        async def _drain() -> None:
            assert self._wakeup is not None
            while True:
                self._wakeup.clear()
                batch = self._take_batch()
                if not batch:
                    await self._wakeup.wait()
                    continue
                for rec in batch:
                    try:
                        await dbmod.insert_jev_decision(self._pool, **rec)
                    except Exception:  # 单条失败不杀 drainer
                        logger.exception(
                            "jev audit insert failed (command=%r)", str(rec.get("command"))[:120]
                        )

        self._task = asyncio.get_running_loop().create_task(_drain(), name="jev-audit-drain")

    async def shutdown(self, timeout_s: float = 5.0) -> None:
        """停止 drainer 并尽力清空队列（宽限内）。"""
        import asyncio

        if self._wakeup is not None:
            self._wakeup.set()
        task, self._task = self._task, None
        if task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._dropped:
            logger.warning("jev audit sink stopped with %d dropped record(s)", self._dropped)
