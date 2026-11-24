"""Jev shell 守卫 + rubric 门控判决（plan: jev-rubric-grader-gate）。

两个出口共用 JevClient 与审计纪律，判决语义各自独立：

  出口一（既有，契约零变化）：`build_shell_guard` → interrupt_on["execute"] 的
  when/description 工厂。三级判决（对一条 execute tool_call）：
     L1 确定性规则（零成本零误判，先于 Jev）：
       - 白名单（只读直通）→ when=False 静默放行，不调 Jev；
       - MCP 前置依赖补齐 / 工作区常规操作（jev_shell_l1_allow，plan: laya-jev-migration
         W1；&& 复合链逐段判定）→ when=False 静默放行；
       - 黑名单（高危模式）→ 跳过 Jev 直接 when=True，description 强标注。
    L2 Jev（白黑之间灰区）：state=当前命令+近期 execute 历史 → 一次并行问全 →
    本地布尔合成（无二次网络调用）：
      - shadow：恒 when=True（行为=现状），判决落库 + description 概率标注；
      - enforce：高置信"常规范围"（工作区写入/脚本运行，非包安装）→ when=False
        静默放行；其余一切（模糊/危险/包安装/失败）→ when=True。
    JevUnavailable → when=True（fail-closed = 现状行为）。

  出口二：`build_rubric_judge` → RubricJudge（judge/ajudge 双入口），供 rubric
  门控 mixin 在 `_invoke_grader`/`_ainvoke_grader` 覆写中调用。先验门控语义
  （每次评分前分诊，含首评 iteration 0；两态 llm|jev 配置即契约，单一"满足时
  通过"极性）：
    - 全部准则 P≥t ∧ conf≥t → fast_pass（合成 satisfied，跳过 LLM grader）；
      首评无上游冻结名单时，合成响应以结构化准则原文兜底，冻结名单由此锚定；
    - 其余一切（含 JevUnavailable）→ 灰区/不可用 → 委派 LLM grader（fail-closed
      = 纯 llm 行为，绝不抛出——异常会触发上游 grader_error 终态）；
    - mode=shadow：恒委派 LLM，判决只落审计（阈值校准/分歧率统计用）。

缓存契约：shell 守卫以 tool_call_id 为键（when/description 双调用契约，plan R7）；
rubric 判决以 (grading_run_id, thread_id, iteration) 为键——thread_id 入键防
run_id 丢失时跨会话碰撞（实测 iter≥1 偶发空 run_id）；覆盖重试由 L1 correction
直通规避，缓存兜底防同键重复判两次。
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


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _path_token_ok(token: str) -> bool:
    """路径 token 安全校验（strict 白名单 / 允许项共享）：剥包裹引号后
    拒绝绝对路径与任何 `..` 段（cwd 即 per-user 工作区）。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        token = token[1:-1]
    if token.startswith(("/", "\\")) or _DRIVE_RE.match(token):
        return False
    return all(seg != ".." for seg in re.split(r"[/\\]", token))


def is_readonly_whitelisted(command: str, *, strict_paths: bool = False) -> bool:
    """白名单判定：首 token 命中 ∧ 参数不含改变语义的运算符。

    git 特例：仅"无论带何参数都无副作用"的子命令放行（status/log/diff/show/
    rev-parse）；branch/tag/remote 带参会创建/删除，config 带参可写——均走 Jev。
    strict_paths=True 时附加逐参数路径校验（绝对路径 / `..` 段即拒）。
    """
    if not command.strip():
        return False
    if _SEMANTIC_OPERATOR_RE.search(command):
        return False
    token = first_token(command)
    rest = _rest_of(command)
    if token == "git":
        sub = rest.strip().split(None, 1)
        if not sub:
            return False
        if sub[0].lower() not in _GIT_READONLY_SUBCOMMANDS:
            return False
    elif token not in READONLY_FIRST_TOKENS:
        return False
    if strict_paths:
        return all(_path_token_ok(t) for t in rest.split())
    return True


def dangerous_match(command: str) -> str | None:
    """黑名单判定：返回命中标签（未命中返回 None）。"""
    for pattern, label in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return label
    return None


# ---------------------------------------------------------------------------
# L1 允许项扩展（plan: laya-jev-migration W1）
#
# 第三类直通：在册 MCP 前置依赖补齐 + 工作区常规操作。放行依据是平台登记
# 事实（mcp_server 表 / JEV_SHELL_ALLOW_PKGS），零误判、零模型参与；与白名单
# （只读）/黑名单（高危）并列，由 jev_shell_l1_allow 总开关控制。
#
# 字符串校验三层防线（允许项是零审批直通行，被构造绕过即 RCE）：
# 1. 全串锚定 `^...$` 正则 + 显式空格分词 + 包名/参数字符集
#    （防 `uvx --from "pkg; rm -rf"`、参数夹带、尾部追加 `&& evil`）；
# 2. 语义操作符预检复用 _SEMANTIC_OPERATOR_RE（`&&` 拆段后逐段再检）；
# 3. 路径仅相对、拒绝任何 `..` 段与绝对路径（cwd 即 per-user 工作区）。
# ---------------------------------------------------------------------------

_PKG = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PKG_RE = re.compile(_PKG)
_FLAG_RE = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_-]*")
_SAFE_ARG_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/=,@+%-~"
)


def _safe_arg_token(token: str) -> bool:
    """允许项参数 token 校验：字符集白名单 + 相对路径 + 无 `..` 段、无引号。"""
    return (
        bool(token)
        and all(c in _SAFE_ARG_CHARS for c in token)
        and not token.startswith(("/", "\\"))
        and not _DRIVE_RE.match(token)
        and all(seg != ".." for seg in re.split(r"[/\\]", token))
    )


# (锚定形态, 是否允许受控尾参)。尾参 = flag 及其值（值过 _safe_arg_token）；
# 裸位置参数视作追加安装的包名，必须同样在册。
MCP_PREREQ_ALLOW: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(rf"^uvx --from (?P<pkg>{_PKG})$"), False),
    (re.compile(rf"^uvx --from (?P<pkg>{_PKG}) [A-Za-z0-9][A-Za-z0-9._-]*$"), False),
    (re.compile(rf"^uv tool install (?P<pkg>{_PKG})$"), False),
    (re.compile(rf"^uv pip install (?P<pkg>{_PKG})(?: (?P<rest>.+))?$"), True),
    (re.compile(rf"^pip install (?P<pkg>{_PKG})$"), False),
    (re.compile(rf"^python3? -m pip install (?P<pkg>{_PKG})(?: (?P<rest>.+))?$"), True),
    (re.compile(rf"^pip3? show (?P<pkg>{_PKG})$"), False),
    (re.compile(rf"^python3? -m pip show (?P<pkg>{_PKG})$"), False),
)

WORKSPACE_OP_FIRST_TOKENS = frozenset({"mkdir", "touch", "cp", "mv", "python", "python3"})


def _prereq_rest_ok(rest: str, allow_pkgs: frozenset[str]) -> bool:
    expect_value = False
    for tok in rest.split(" "):
        if not tok:
            return False
        if expect_value:
            if not _safe_arg_token(tok):
                return False
            expect_value = False
        elif tok.startswith("-"):
            if not _FLAG_RE.fullmatch(tok):
                return False
            expect_value = True
        elif not (_PKG_RE.fullmatch(tok) and tok.lower() in allow_pkgs):
            return False
    return True


def mcp_prereq_whitelisted(command: str, allow_pkgs: frozenset[str]) -> bool:
    """在册 MCP 前置依赖补齐判定：形态命中 ∧ 包名在册 ∧ 尾参受控。"""
    if not allow_pkgs or "'" in command or '"' in command:
        return False
    if _SEMANTIC_OPERATOR_RE.search(command):
        return False
    for pattern, allow_rest in MCP_PREREQ_ALLOW:
        m = pattern.match(command)
        if m is None or m.group("pkg").lower() not in allow_pkgs:
            continue
        rest = m.groupdict().get("rest")
        if rest and not (allow_rest and _prereq_rest_ok(rest, allow_pkgs)):
            return False
        return True
    return False


def _python_script_args(args: list[str]) -> bool:
    """python 工作区形态：`python [flags] script.py [args]`（排除 -m/-c 模块与
    内联执行——模块形态仅经 MCP_PREREQ_ALLOW 的 pip show/install 覆盖）。"""
    if any(t.startswith(("-m", "-c")) for t in args):
        return False
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1
    return i < len(args) and args[i].endswith(".py")


def workspace_op_whitelisted(command: str) -> bool:
    """工作区常规操作判定：首 token 命中 ∧ 全参数过字符集/路径校验。"""
    if "'" in command or '"' in command or _SEMANTIC_OPERATOR_RE.search(command):
        return False
    parts = command.split()
    token = parts[0].lower() if parts else ""
    if token not in WORKSPACE_OP_FIRST_TOKENS:
        return False
    args = parts[1:]
    if not args or not all(_safe_arg_token(t) for t in args):
        return False
    if token in ("python", "python3"):
        return _python_script_args(args)
    return True


_CHAIN_SPLIT_RE = re.compile(r"\s*&&\s*")
_L1_SOURCE_BY_RANK = ("rule_whitelist", "rule_workspace", "rule_mcp_allow")


def l1_extension_match(command: str, allow_pkgs: frozenset[str]) -> str | None:
    """L1 扩展面总判定（含 `&&` 复合链逐段）：返回放行 source，None=落 L2。

    任一段不属 L1 允许面（只读 / MCP 前置 / 工作区）→ 整链落 L2；
    source 取链内最强类别（mcp > workspace > 只读）。
    """
    if "'" in command or '"' in command:
        return None
    segments = [s for s in _CHAIN_SPLIT_RE.split(command.strip()) if s]
    if not segments:
        return None
    rank = 0
    for seg in segments:
        if is_readonly_whitelisted(seg, strict_paths=True):
            continue
        if mcp_prereq_whitelisted(seg, allow_pkgs):
            rank = 2
        elif workspace_op_whitelisted(seg):
            rank = max(rank, 1)
        else:
            return None
    return _L1_SOURCE_BY_RANK[rank]


def parse_prereq_packages(server_rows: list[dict]) -> frozenset[str]:
    """mcp_server 行集 → 前置依赖包名集（command/args 中 `--from` / `install`
    形态，lowercase）。enabled=false 的 server 不参与。"""
    pkgs: set[str] = set()
    for row in server_rows or []:
        if not row.get("enabled", True):
            continue
        cfg = row.get("config_json")
        if not isinstance(cfg, dict):
            continue
        tokens = [str(cfg.get("command") or "").strip(), *(str(a) for a in cfg.get("args") or [])]
        tokens = [t for t in tokens if t]
        for i, tok in enumerate(tokens):
            low = tok.lower()
            if low == "--from":
                nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
                if _PKG_RE.fullmatch(nxt):
                    pkgs.add(nxt.lower())
            elif low == "install":
                for nxt in tokens[i + 1 :]:
                    if nxt.startswith("-") or not _PKG_RE.fullmatch(nxt):
                        break
                    pkgs.add(nxt.lower())
    return frozenset(pkgs)


def parse_pkg_list(raw: str) -> frozenset[str]:
    """逗号分隔包名串（JEV_SHELL_ALLOW_PKGS）→ frozenset；非法 token 丢弃并告警。"""
    out: set[str] = set()
    for tok in (raw or "").replace("，", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if _PKG_RE.fullmatch(tok):
            out.add(tok.lower())
        else:
            logger.warning("jev_shell_allow_pkgs: 非法包名已忽略 %r", tok)
    return frozenset(out)


# ---------------------------------------------------------------------------
# State rendering (plan §五.2: 当前命令 + 近期 execute 历史)
# ---------------------------------------------------------------------------

_RECENT_HISTORY_MESSAGES = 60  # 扫描窗口（条），足够覆盖多步链又控制渲染成本
_HISTORY_RESULT_CHARS = 160    # 单条结果摘要截断


def render_state(messages: Any, command: str, max_chars: int) -> str:
    """构建送判 state：待评估命令 + 近期 execute 历史（截断只裁历史，命令恒在）。

    结果按 tool_call_id 归属（execute 调用产生的 ToolMessage 才入摘要），
    多步链意识（plan §五.2 / R3）。
    """
    head = f"待评估命令: {command}"
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
    history = "（近期 shell 命令历史，最新在最后）\n" + "\n".join(lines) if lines else ""
    budget = max_chars - len(head) - 32
    if budget <= 0:
        history = ""
    elif len(history) > budget:
        # 只裁最早的历史，待评估命令恒在
        history = "…(已截断)\n" + history[-budget:]
    return f"{head}\n\n{history}".rstrip()


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
    allow_pkgs: frozenset[str] = frozenset(),
) -> tuple[Callable[[Any], bool], Callable[[Any, Any, Any], str]]:
    """构建 (when_fn, description_fn) 二元组，供 interrupt_on["execute"] 注入。

    Args:
        settings: Settings（读取 mode/threshold/state_max_chars 等；阈值随图构建
            烘焙，改配置需重启后端或等 registry TTL 过期——plan §三/R10）。
        audit_sink: 审计回调（dict → None）；None 时只打日志不落库。
        agent_id: 构建期烘焙（审计表 NOT NULL 列）。
        allow_pkgs: 在册 MCP 前置依赖包名集（agent_registry 从 mcp_server 表
            解析注入；与 JEV_SHELL_ALLOW_PKGS 并集后烘焙，TTL 语义同上）。
    """
    client = JevClient(
        base_url=settings.jev_base_url,
        api_key=settings.typesafe_api_key,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_seconds,
        proxy_url=getattr(settings, "jev_proxy_url", ""),
    )
    mode = settings.jev_mode
    threshold = settings.jev_safe_threshold
    state_max_chars = settings.jev_state_max_chars
    l1_ext = bool(getattr(settings, "jev_shell_l1_allow", True))
    pkgs = frozenset(p.lower() for p in allow_pkgs) | parse_pkg_list(
        getattr(settings, "jev_shell_allow_pkgs", "")
    )

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
        logger.info(
            "jev gate | agent=%s verdict=%s source=%s cmd=%r",
            agent_id, record["verdict"], decision.source, command[:120],
        )
        if audit_sink is not None:
            audit_sink(record)

    def _judge(command: str, req: Any) -> GuardDecision:
        # L1 白名单：只读直通（不调 Jev；source=rule_whitelist 落库供灰度对账）
        if is_readonly_whitelisted(command, strict_paths=l1_ext):
            return GuardDecision(source="rule_whitelist", ask_human=False)
        # L1 允许项扩展：在册 MCP 前置补齐 / 工作区常规操作（&& 链逐段判定）
        if l1_ext:
            src = l1_extension_match(command, pkgs)
            if src is not None:
                return GuardDecision(source=src, ask_human=False)
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


# ---------------------------------------------------------------------------
# Rubric 门控判决器（出口二，plan: jev-rubric-grader-gate）
# ---------------------------------------------------------------------------


@dataclass
class RubricDecision:
    """一次 rubric 门控判决（缓存 + 审计的载荷）。

    `fast_pass` 只在 `source == "jev"` 时有意义；`unavailable` 一律
    fast_pass=False（调用方委派 LLM grader，fail-closed）。
    """

    source: str  # jev | unavailable
    fast_pass: bool = False
    annotation: str | None = None  # 概率摘要（合成 GraderResponse.explanation 用）
    state_rendered: str | None = None
    answers: dict | None = None
    raw: dict | None = None
    latency_ms: int | None = None
    model: str | None = None


def _synthesize_rubric(answers: dict, crit_meta: dict[str, dict]) -> bool:
    """本地布尔合成（无二次网络调用）——单一语义：满足时通过。

    每条准则一个 noul 题（statement=达标断言，P(yes) 高=满足）：
    全部准则 P ≥ t ∧ conf ≥ t → fast_pass；任一不达标 → 灰区委派 LLM。
    危险类要求以正向达标式书写（"引用真实来源"而非"不编造"），措辞方向
    与判定方向天然一致，无极性登记负担。
    """
    for qid, meta in crit_meta.items():
        ans = answers.get(qid) or {}
        t = float(meta["threshold"])
        p = float(ans.get("noul", 0.0))
        conf = float(ans.get("confidence", 0.0))
        if not (p >= t and conf >= t):
            return False
    return True


def _format_rubric_probs(answers: dict, crit_meta: dict[str, dict]) -> str:
    """逐题概率摘要（审计日志与合成 explanation 共用）。"""
    return " ".join(
        f"{qid}={float((answers.get(qid) or {}).get('noul', 0.0)):.2f}"
        f"/conf={float((answers.get(qid) or {}).get('confidence', 0.0)):.2f}"
        for qid in crit_meta
    )


class RubricJudge:
    """rubric 先验门控判决器（每 rubric 节点一个实例，随图构建烘焙问题集/阈值/mode）。

    判决缓存以 (grading_run_id, thread_id, iteration) 为键——对应 shell 守卫
    tool_call_id 的"同一调用严禁判两次"纪律。fail-closed：client 任何异常统一
    归一为 `RubricDecision(source="unavailable")`（调用方据此委派 LLM grader），
    绝不向上抛出——异常会触发上游 `after_agent` 的 grader_error 终态。

    mode（enforce|shadow）只约束调用方是否允许快道短路，判决与审计两路径
    逐字段一致——shadow 是校准态：判了、落审计、不改变行为。

    state 不截断（回合完整性优先）：超长 state 由 API 拒绝 → JevUnavailable →
    委派 LLM——宁可不判，不发残卷。审计中的 state_rendered 为全量原文。
    """

    def __init__(
        self,
        client,
        audit_submit: Callable[[dict], None] | None,
        agent_id: uuid.UUID,
        questions: dict,
        crit_meta: dict[str, dict],
        mode: str = "enforce",
    ):
        self._client = client
        self._audit_submit = audit_submit
        self._agent_id = agent_id
        self._questions = questions
        self._crit_meta = crit_meta
        self._mode = mode
        self._cache: OrderedDict[tuple, RubricDecision] = OrderedDict()
        self._cache_lock = threading.Lock()

    @property
    def mode(self) -> str:
        """运行态（enforce|shadow）：shadow 下调用方不得快道短路（只判只落审计）。"""
        return self._mode

    @property
    def criteria_statements(self) -> list[str]:
        """准则原文列表（合成 GraderResponse 的兜底名字，防御性用途）。"""
        return [m["statement"] for m in self._crit_meta.values()]

    # ---------------- 缓存（同 shell 守卫纪律：同键只判一次） ----------------
    def _cache_get(self, key: tuple) -> RubricDecision | None:
        with self._cache_lock:
            return self._cache.get(key)

    def _cache_put(self, key: tuple, decision: RubricDecision) -> None:
        with self._cache_lock:
            self._cache[key] = decision
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    # ---------------- 判决（同步/异步双入口，语义逐字段一致） ----------------
    def _finalize(
        self,
        state_text: str,
        verdict: JevVerdict,
        key: tuple,
        thread_id: str,
    ) -> RubricDecision:
        fast_pass = _synthesize_rubric(verdict.answers, self._crit_meta)
        annotation = _format_rubric_probs(verdict.answers, self._crit_meta)
        tail = "全准则达标 → 建议快道放行" if fast_pass else "未达快道阈值 → 委派 LLM grader"
        return RubricDecision(
            source="jev",
            fast_pass=fast_pass,
            annotation=f"[Jev] {annotation} → {tail}",
            state_rendered=state_text,
            answers=verdict.answers,
            raw=verdict.raw,
            latency_ms=verdict.latency_ms,
            model=verdict.model,
        )

    def _unavailable(self, state_text: str, err: Exception) -> RubricDecision:
        logger.warning("jev rubric judge unavailable (fail-closed → delegate LLM): %s", err)
        return RubricDecision(
            source="unavailable",
            annotation=f"[Jev 不可用，委派 LLM grader] {err}",
            state_rendered=state_text,
        )

    def _audit(self, decision: RubricDecision, key: tuple, thread_id: str) -> None:
        verdict = "auto_pass" if decision.fast_pass else "ask_llm"
        record = {
            "decision_type": "rubric",
            "session_id": None,  # 会话行与 thread 的映射在业务层，门控层只有 thread_id
            "thread_id": thread_id or "unknown",
            "agent_id": self._agent_id,
            # 复用 NOT NULL 列承载判决键（rubric 判决无 tool_call 概念）；
            # run_id 丢失（空串）时回退 thread_id，保键可读且非空
            "tool_call_id": f"rubric:{key[0] or thread_id}:{key[-1]}",
            "mode": self._mode,
            "source": decision.source,
            "verdict": verdict,
            "command": None,
            "state_rendered": decision.state_rendered,
            "answers": {**(decision.answers or {}), "_raw": decision.raw}
            if decision.answers is not None
            else None,
            "latency_ms": decision.latency_ms,
            "model": decision.model,
        }
        logger.info(
            "jev rubric gate | agent=%s verdict=%s source=%s key=%r%s",
            self._agent_id, verdict, decision.source, key,
            f" | {decision.annotation}" if decision.annotation else "",
        )
        if self._audit_submit is not None:
            self._audit_submit(record)

    def judge(
        self,
        state_text: str,
        key: tuple,
        thread_id: str = "",
    ) -> RubricDecision:
        """同步判决（`_invoke_grader` 覆写调用）。缓存命中直接返回。"""
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            verdict = self._client.judge(state_text, self._questions)
        except JevUnavailable as e:
            decision = self._unavailable(state_text, e)
        else:
            decision = self._finalize(state_text, verdict, key, thread_id)
        self._cache_put(key, decision)
        self._audit(decision, key, thread_id)
        return decision

    async def ajudge(
        self,
        state_text: str,
        key: tuple,
        thread_id: str = "",
    ) -> RubricDecision:
        """异步判决（`_ainvoke_grader` 覆写调用，走 classifier.ainvoke 不阻塞 loop）。"""
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            verdict = await self._client.ajudge(state_text, self._questions)
        except JevUnavailable as e:
            decision = self._unavailable(state_text, e)
        else:
            decision = self._finalize(state_text, verdict, key, thread_id)
        self._cache_put(key, decision)
        self._audit(decision, key, thread_id)
        return decision


def build_rubric_judge(
    settings: Any,
    audit_submit: Callable[[dict], None] | None,
    agent_id: uuid.UUID,
    questions: dict,
    crit_meta: dict[str, dict],
) -> RubricJudge:
    """构建 rubric 门控判决器（与 build_shell_guard 平行的出口）。

    Args:
        settings: Settings（client/阈值随构建烘焙，改配置需重启或等 registry
            TTL 过期——与 shell 守卫同语义；state 不截断，超限走 API 异常回退）。
        audit_submit: 审计回调（dict → None）；None 时只打日志不落库。
        agent_id: 构建期烘焙（审计表 NOT NULL 列）。
        questions: 按节点 criteria 渲染的问题集（langchain_typesafe Question 对象）。
        crit_meta: 逐题合成元数据（polarity/qtype/threshold/veto/pass_option/
            statement），由 core_utils.render_jev_criteria 产出。
    """
    client = JevClient(
        base_url=settings.jev_base_url,
        api_key=settings.typesafe_api_key,
        model=settings.jev_model,
        timeout_s=settings.jev_timeout_seconds,
        proxy_url=getattr(settings, "jev_proxy_url", ""),
    )
    # 阈值三级解析的最后一跳：行级 ← 节点级（render_jev_criteria 已消费）←
    # 平台级默认（此处烘焙）。crit_meta 为浅拷贝，不污染调用方。
    default_t = float(getattr(settings, "jev_rubric_pass_threshold", 0.9))
    resolved_meta = {
        qid: (
            {**meta, "threshold": default_t}
            if meta.get("threshold") is None
            else {**meta, "threshold": float(meta["threshold"])}
        )
        for qid, meta in crit_meta.items()
    }
    return RubricJudge(
        client,
        audit_submit,
        agent_id,
        questions,
        resolved_meta,
        mode=getattr(settings, "jev_rubric_mode", "enforce"),
    )
