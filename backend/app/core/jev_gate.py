"""Jev shell 守卫：`interrupt_on["execute"]` 的 when/description 工厂（plan §五）。

三级判决（对一条 execute tool_call）：
  L1 确定性规则（零成本零误判，先于 Jev）：
    - 白名单（只读直通）→ when=False 静默放行，不调 Jev；
    - 黑名单（高危模式）→ 跳过 Jev 直接 when=True，description 强标注。
  L2 Jev（白黑之间灰区）：state=当前命令+近期 execute 历史 → 一次并行问全 →
    本地布尔合成（无二次网络调用）：
    - shadow：恒 when=True（行为=现状），判决落库 + description 概率标注；
    - enforce：高置信"常规范围"（工作区写入/脚本运行，非包安装）→ when=False
      静默放行；其余一切（模糊/危险/包安装/失败）→ when=True。
  JevUnavailable → when=True（fail-closed = 现状行为）。

缓存契约（plan R7）：`when` 与 `description` 是 HumanInTheLoopMiddleware 的两次
独立同步调用（_should_interrupt → when 先写缓存；_create_action_and_config →
description 后读缓存），以 tool_call_id 为键，严禁对同一调用判两次 Jev。
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.services.jev_client import JevClient, JevUnavailable, JevVerdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# L1 deterministic rules
# ---------------------------------------------------------------------------

# 只读白名单：首个 token 命中且参数不含改变语义的运算符 → 直通。
# 覆盖 POSIX 与 PowerShell 常用只读命令（大小写不敏感）。
READONLY_FIRST_TOKENS = frozenset(
    {
        # POSIX
        "pwd", "ls", "dir", "cd", "echo", "cat", "type", "whoami", "tree",
        "head", "tail", "wc", "which", "whereis", "hostname", "date", "env", "printenv",
        "uname", "id", "groups", "file", "stat", "du", "df",
        # PowerShell（不区分大小写，统一小写登记）
        "get-childitem", "get-location", "get-content", "get-item", "get-date",
        "get-command", "get-help", "get-variable",
        # git 只读子命令经 _GIT_READONLY 单独刻画（见下）
    }
)

# git 只读子命令：`git status` / `git log` / `git diff` 等——无论带何参数都无副作用
# （branch/tag/remote 带参会创建/删除，config 带参可写全局配置，均不入白名单）。
_GIT_READONLY_SUBCOMMANDS = frozenset({"status", "log", "diff", "show", "rev-parse"})

# 改变语义的运算符：命中则白名单失效（`ls > x` / `cat a | rm` / `cd dir && rm` …）。
# `;` `&` `|` `>` `<` 反引号、$() 命令替换（( ) 用于 $() 内已含，括号单独保留）。
_SEMANTIC_OPERATOR_RE = re.compile(r"[<>|&;`()$]")

# 高危黑名单（正则，忽略大小写）：命中 → 跳过 Jev 直接问人 + 强标注。
# 每项 (pattern, label)；宁可误报问人，不可漏放。
DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), label)
    for p, label in (
        (r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", "递归强制删除 (rm -rf)"),
        (r"\brmdir\s+/s\b", "递归删除目录 (rmdir /s)"),
        (r"\bdel\s+/[a-z]*s", "递归删除 (del /s)"),
        (r"\berase\s+/[a-z]*s", "递归删除 (erase /s)"),
        (r"\bformat\b", "格式化磁盘 (format)"),
        (r"\bshutdown\b|\breboot\b|\brestart\b", "关机/重启"),
        (r"\breg\s+(delete|add)\b", "注册表写操作 (reg)"),
        (r"(curl|wget|irm|iwr)[^|;&]*(\||;)\s*(sudo\s+)?(ba)?sh\b", "下载并执行 (curl|sh)"),
        (r"\bmkfs(\.\w+)?\b", "创建文件系统 (mkfs)"),
        (r"\bdd\s+if=", "裸磁盘写入 (dd)"),
        (r">\s*/dev/sd[a-z]", "直写块设备"),
        (r"\bchmod\s+(-[a-z]+\s+)?777\b", "全开权限 (chmod 777)"),
        (r"\bnet\s+user\b", "账户管理 (net user)"),
        (r"\bsc\s+(delete|config)\b", "系统服务变更 (sc)"),
        (r"\bremove-item\s+.*-recurse", "递归删除 (Remove-Item -Recurse)"),
    )
)


def first_token(command: str) -> str:
    """命令首 token（小写、去路径前缀与引号），用于白名单匹配。"""
    token = command.strip().split(None, 1)[0] if command.strip() else ""
    token = token.strip("\"'")
    # 去掉路径前缀（./git、C:\tools\git.exe → git）
    for sep in ("/", "\\"):
        token = token.rsplit(sep, 1)[-1]
    if token.lower().endswith(".exe"):
        token = token[:-4]
    return token.lower()


def _rest_of(command: str) -> str:
    parts = command.strip().split(None, 1)
    return parts[1] if len(parts) > 1 else ""


def is_readonly_whitelisted(command: str) -> bool:
    """白名单判定：首 token 命中 ∧ 参数不含改变语义的运算符。

    git 特例：仅"无论带何参数都无副作用"的子命令放行（status/log/diff/show/
    rev-parse）；branch/tag/remote 带参会创建/删除，config 带参可写——均走 Jev。
    """
    if not command.strip():
        return False
    rest = _rest_of(command)
    if _SEMANTIC_OPERATOR_RE.search(command):
        return False
    token = first_token(command)
    if token == "git":
        sub = rest.strip().split(None, 1)
        if not sub:
            return False
        return sub[0].lower() in _GIT_READONLY_SUBCOMMANDS
    return token in READONLY_FIRST_TOKENS


def dangerous_match(command: str) -> str | None:
    """黑名单判定：返回命中标签（未命中返回 None）。"""
    for pattern, label in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return label
    return None


# ---------------------------------------------------------------------------
# State rendering (plan §五.2: 当前命令 + 近期 execute 历史)
# ---------------------------------------------------------------------------

_RECENT_HISTORY_MESSAGES = 60  # 扫描窗口（条），足够覆盖多步链又控制渲染成本
_HISTORY_RESULT_CHARS = 160    # 单条结果摘要截断


def render_state(messages: Any, command: str, max_chars: int) -> str:
    """构建送判 state：近期 execute 命令/结果摘要 + 待评估命令，截断至 max_chars。

    结果按 tool_call_id 归属（execute 调用产生的 ToolMessage 才入摘要），
    多步链意识（plan §五.2 / R3）。
    """
    lines: list[str] = []
    try:
        iterable = list(messages or [])[-_RECENT_HISTORY_MESSAGES:]
    except TypeError:
        iterable = []
    execute_call_ids: set[str] = set()
    for msg in iterable:
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict) and tc.get("name") == "execute":
                execute_call_ids.add(str(tc.get("id") or ""))
                lines.append(f"$ {(tc.get('args') or {}).get('command') or ''!s}")
        if getattr(msg, "type", None) == "tool" and str(
            getattr(msg, "tool_call_id", "") or ""
        ) in execute_call_ids:
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                lines.append(f"→ {content[:_HISTORY_RESULT_CHARS]}")
    state_text = (
        "（近期 shell 命令历史，最新在最后）\n"
        + "\n".join(lines)
        + f"\n\n待评估命令: {command}"
    )
    if len(state_text) > max_chars:
        state_text = state_text[:max_chars] + "\n…(已截断)"
    return state_text


# ---------------------------------------------------------------------------
# Verdict synthesis (plan §五.3)
# ---------------------------------------------------------------------------


@dataclass
class GuardDecision:
    """一条 execute 调用的完整判决（缓存 + 审计的载荷）。"""

    source: str  # rule_whitelist | rule_blacklist | jev | unavailable
    ask_human: bool
    annotation: str | None = None  # description 工厂拼接用（source=jev 时含概率）
    state_rendered: str | None = None
    answers: dict | None = None  # JevVerdict.answers（归一化）
    raw: dict | None = None  # API 原始 answers + usage（审计留痕）
    latency_ms: int | None = None
    model: str | None = None
    suggest_auto: bool = False  # 合成判定是否建议放行（shadow 标注用）


def synthesize(verdict: JevVerdict, threshold: float, mode: str) -> tuple[bool, bool]:
    """本地布尔合成（无二次网络调用）。

    极性表（plan R6）：
      正向题 scope_routine：P ≥ T ∧ conf ≥ T（高 P = 安全）
      反向题 touches_system：P ≤ 1−T ∧ conf ≥ T（高 P = 危险，反向正交否决）
      选项题 risk_tier：P(safe) ≥ T ∧ conf ≥ T
      反向题 installs_packages：P ≥ T → 无条件问人（绝对否决，压过一切正向分）
    Returns:
        (ask_human, suggest_auto)——shadow 模式 ask 恒 True，suggest_auto 仍按
        enforce 语义计算（供标注与灰度统计）。
    """
    answers = verdict.answers
    t = threshold

    def _noul(qid: str) -> tuple[float, float]:
        ans = answers.get(qid) or {}
        return float(ans.get("noul", 0.0)), float(ans.get("confidence", 0.0))

    scope_p, scope_c = _noul("scope_routine")
    system_p, system_c = _noul("touches_system")
    install_p, _ = _noul("installs_packages")
    risk = answers.get("risk_tier") or {}
    risk_safe_p = float((risk.get("probabilities") or {}).get("safe", 0.0))
    risk_c = float(risk.get("confidence", 0.0))

    installs_veto = install_p >= t
    passes = (
        scope_p >= t
        and scope_c >= t
        and system_p <= 1.0 - t
        and system_c >= t
        and risk_safe_p >= t
        and risk_c >= t
    )
    suggest_auto = passes and not installs_veto
    ask = True if mode != "enforce" else (not suggest_auto)
    return ask, suggest_auto


def _format_probs(verdict: JevVerdict) -> str:
    a = verdict.answers
    scope = a.get("scope_routine") or {}
    system = a.get("touches_system") or {}
    risk = a.get("risk_tier") or {}
    safe_p = (risk.get("probabilities") or {}).get("safe", float("nan"))
    confs = [
        float((a.get(k) or {}).get("confidence", 0.0))
        for k in ("scope_routine", "touches_system", "risk_tier")
    ]
    return (
        f"routine={scope.get('noul', float('nan')):.2f} "
        f"system={system.get('noul', float('nan')):.2f} "
        f"safe={safe_p:.2f} "
        f"conf={min(confs):.2f}"
    )


# ---------------------------------------------------------------------------
# Gate factory
# ---------------------------------------------------------------------------

_CACHE_MAX_ENTRIES = 1024  # 判决缓存上限（防长驻图无界增长）


def build_shell_guard(
    settings: Any,
    audit_sink: Callable[[dict], None] | None,
    agent_id: uuid.UUID,
) -> tuple[Callable[[Any], bool], Callable[[Any, Any, Any], str]]:
    """构建 (when_fn, description_fn) 二元组，供 interrupt_on["execute"] 注入。

    Args:
        settings: Settings（读取 mode/threshold/state_max_chars 等；阈值随图构建
            烘焙，改配置需重启后端或等 registry TTL 过期——plan §三/R10）。
        audit_sink: 审计回调（dict → None）；None 时只打日志不落库。
        agent_id: 构建期烘焙（审计表 NOT NULL 列）。
    """
    client = JevClient(
        base_url=settings.jev_base_url,
        api_key=settings.typesafe_api_key,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_seconds,
    )
    mode = settings.jev_mode
    threshold = settings.jev_safe_threshold
    state_max_chars = settings.jev_state_max_chars

    cache: OrderedDict[str, GuardDecision] = OrderedDict()
    cache_lock = threading.Lock()

    def _cache_put(key: str, decision: GuardDecision) -> None:
        with cache_lock:
            cache[key] = decision
            while len(cache) > _CACHE_MAX_ENTRIES:
                cache.popitem(last=False)

    def _cache_get(key: str) -> GuardDecision | None:
        with cache_lock:
            return cache.get(key)

    def _thread_id(req: Any) -> str:
        configurable = ((getattr(getattr(req, "runtime", None), "config", None) or {})
                        .get("configurable") or {})
        return str(configurable.get("thread_id") or "")

    def _audit(command: str, decision: GuardDecision, thread_id: str, tool_call_id: str) -> None:
        record = {
            "session_id": None,  # 会话行与 thread 的映射在业务层，门控层只有 thread_id
            "thread_id": thread_id or "unknown",
            "agent_id": agent_id,
            "tool_call_id": tool_call_id,
            "mode": mode,
            "source": decision.source,
            "verdict": "ask_human" if decision.ask_human else "auto_run",
            "command": command,
            "state_rendered": decision.state_rendered,
            "answers": {**(decision.answers or {}), "_raw": decision.raw}
            if decision.answers is not None
            else None,
            "latency_ms": decision.latency_ms,
            "model": decision.model,
        }
        if audit_sink is not None:
            audit_sink(record)
        else:
            logger.info(
                "jev gate | agent=%s verdict=%s source=%s cmd=%r",
                agent_id, record["verdict"], decision.source, command[:120],
            )

    def _judge(command: str, req: Any) -> GuardDecision:
        # L1 白名单：只读直通（不调 Jev；source=rule_whitelist 落库供灰度对账）
        if is_readonly_whitelisted(command):
            return GuardDecision(source="rule_whitelist", ask_human=False)
        # L1 黑名单：高危直接问人 + 强标注（省一次调用，人必审）
        hit = dangerous_match(command)
        if hit is not None:
            return GuardDecision(
                source="rule_blacklist",
                ask_human=True,
                annotation=f"[规则] 命中高危命令模式：{hit}——必须人工审批",
            )
        # L2 Jev 灰区（state 为 AgentState TypedDict：消息在 "messages" 键下）
        state_map = getattr(req, "state", None) or {}
        try:
            messages = state_map.get("messages") or []
        except AttributeError:
            messages = []
        state_rendered = render_state(messages, command, state_max_chars)
        try:
            verdict = client.judge(state_rendered)
        except JevUnavailable as e:
            logger.warning("jev unavailable (fail-closed → ask human): %s", e)
            return GuardDecision(
                source="unavailable",
                ask_human=True,
                annotation=f"[Jev 不可用，回落人工审批] {e}",
                state_rendered=state_rendered,
            )
        ask, suggest_auto = synthesize(verdict, threshold, mode)
        if suggest_auto:
            tail = "建议放行（enforce 模式下将自动执行）"
        else:
            tail = "未达自动放行阈值，建议人工审批"
        return GuardDecision(
            source="jev",
            ask_human=ask,
            annotation=f"[Jev] {_format_probs(verdict)} → {tail}",
            state_rendered=state_rendered,
            answers=verdict.answers,
            raw=verdict.raw,
            latency_ms=verdict.latency_ms,
            model=verdict.model,
            suggest_auto=suggest_auto,
        )

    # ---------------- when：HumanInTheLoopMiddleware._should_interrupt 调用 ----------------
    def when(req: Any) -> bool:
        tool_call = getattr(req, "tool_call", None) or {}
        try:
            command = str((tool_call.get("args") or {}).get("command") or "")
        except AttributeError:
            command = ""
        tool_call_id = str(tool_call.get("id") or "")
        decision = _judge(command, req)
        _cache_put(tool_call_id, decision)
        _audit(command, decision, _thread_id(req), tool_call_id)
        return decision.ask_human

    # -------- description：_create_action_and_config 调用（须读缓存，plan 第②次介入） --------
    def description(tool_call: Any, state: Any, runtime: Any) -> str:
        decision = _cache_get(str((tool_call or {}).get("id") or ""))
        if decision is not None and decision.annotation:
            return decision.annotation
        return "Shell 命令执行需要人工审批"

    return when, description
