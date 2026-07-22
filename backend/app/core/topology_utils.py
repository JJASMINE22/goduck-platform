"""子代理委派拓扑可视化 (migrated from DeepAgentsDemos topology_utils.py).

deepagents 把子代理封装进单个 `task` 工具, 对主图的静态拓扑不可见, 因此无法用
`agent.get_graph().draw_mermaid_png()` 看到"谁委派给谁"。但 *委派关系* 本身就完整
存在于 `AgentConfig` 这棵强类型配置树里 —— 无需任何运行时闭包解析。

本模块以 `AgentConfig` 为唯一数据源, 合成一张逻辑委派树 (mermaid 文本 + ASCII):
  - 每个节点标注其角色 (main / compiled / dicted 叶子) 与关键能力
  - 父子之间用 `task` 边表示运行时的委派机制

对外两个入口:
  - build_delegation_mermaid : 纯内存, 返回 mermaid 文本 (API 用, 不碰文件)
  - render_delegation_tree   : 文件版 (.mmd/.png + 控制台), CLI/seed 用
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from langchain_core.runnables.graph_mermaid import draw_mermaid_png

from app.core.schema_utils import AgentConfig

logger = logging.getLogger(__name__)


# ======================== 主题 / 布局 / 形状 ========================
# 通过 %%{init:...}%% 指令注入 (frontmatter config 在 mermaid.ink 上会 400, 不能用)。
_MERMAID_INIT = "%%{init: {'theme':'neo','flowchart':{'defaultRenderer':'elk'}}}%%"

_SHAPE_BY_ROLE = {
    "main": "rounded",
    "compiled": "rounded",
    "dicted": "stadium",
    "ghost": "rounded",
}


# ======================== 角色配色 ========================
_MERMAID_STYLES = """
classDef main fill:#1f6feb,color:#ffffff,stroke:#0d47a1,stroke-width:2px;
classDef compiled fill:#fef3c7,color:#1f2937,stroke:#d97706,stroke-width:1px;
classDef dicted fill:#f3e8ff,color:#1f2937,stroke:#9333ea,stroke-width:1px;
classDef ghost fill:#f1f5f9,color:#64748b,stroke:#94a3b8,stroke-width:1px,stroke-dasharray:4 3;
""".strip()


def _role(node: AgentConfig, is_main: bool) -> str:
    if is_main:
        return "main"
    return "dicted" if node.is_dicted else "compiled"


def _skill_short(path: str) -> str:
    p = path.strip("/")
    return p.split("/")[-1] if "/" in p else p


_DIVIDER = "────────────────────────────"
_BULLET = "▸ "


def _cap_join(items: List[str], max_items: int) -> str:
    """把列表拼成逗号分隔串, 超过 max_items 项时截断并追加 (+N) 提示。"""
    if len(items) <= max_items:
        return ", ".join(items)
    shown = ", ".join(items[:max_items])
    return f"{shown}  (+{len(items) - max_items})"


def _escape(text: str) -> str:
    return text.replace('"', "'")


def _node_lines(node: AgentConfig, is_main: bool, max_items: int = 4) -> List[str]:
    """合成单个节点的 mermaid 标签行 (标题 + 分隔线 + 要点, 已转义)。"""
    name = node.name or "root"
    role = "main gateway" if is_main else ("dicted · leaf" if node.is_dicted else "compiled")

    lines: List[str] = [f"{name}  〔{role}〕", _DIVIDER]

    facts: List[str] = []
    if node.tool_names:
        facts.append("tools: " + _cap_join(list(node.tool_names), max_items))

    if node.skills:
        facts.append("skills: " + _cap_join([_skill_short(s) for s in node.skills], max_items))

    if node.rubric and (node.rubric.content or "").strip():
        facts.append("rubric: " + ("fixed" if node.rubric.fixed else "on-demand"))

    if node.interrupt_on:
        hitl = [k for k, v in node.interrupt_on.items() if v]
        if hitl:
            facts.append("hitl: " + _cap_join(hitl, max_items))

    if is_main:
        mem = "memory ✓" if node.memory else "memory (default)"
        ws = f"workspace: {node.workspace}" if node.workspace else "workspace (default)"
        facts.append(f"{mem}  ·  {ws}")

    lines.extend(_BULLET + f for f in facts)
    return [_escape(line) for line in lines]


def _ascii_tree(node: AgentConfig, is_main: bool, prefix: str, is_last: bool) -> List[str]:
    branch = "\\-- " if is_last else "+-- "
    name = node.name or "root"
    role = "main gateway" if is_main else ("dicted leaf" if node.is_dicted else "compiled")
    head = f"{prefix}{branch}{name}  [{role}]"
    facts: List[str] = []
    if node.tool_names:
        facts.append("tools: " + ", ".join(node.tool_names[:5]))
    if node.skills:
        facts.append("skills: " + ", ".join(_skill_short(s) for s in node.skills))
    if node.rubric and (node.rubric.content or "").strip():
        facts.append("rubric ✓")
    if node.interrupt_on:
        ks = [k for k, v in node.interrupt_on.items() if v]
        if ks:
            facts.append("interrupt: " + ", ".join(ks))
    out = [head + ("    " + " | ".join(facts) if facts else "")]
    children = node.subagents or []
    for i, child in enumerate(children):
        child_prefix = prefix + ("    " if is_last else "|   ")
        out.extend(_ascii_tree(child, False, child_prefix, i == len(children) - 1))
    return out


# ======================== mermaid 合成 ========================
def _build_mermaid(
    root: AgentConfig,
    *,
    show_general_purpose: bool,
    max_items: int = 4,
    edge_label: str = "",
) -> Tuple[str, List[str]]:
    """合成整棵树的 mermaid 文本。返回 (mermaid, ascii_tree_lines)。"""
    counter = {"i": 0}

    def _nid() -> str:
        counter["i"] += 1
        return f"n{counter['i']}"

    def _edge(src: str, dst: str, dashed: bool = False) -> str:
        arrow = "-.->" if dashed else "-->"
        if edge_label:
            return f"{src} {arrow}|{edge_label}| {dst}"
        return f"{src} {arrow} {dst}"

    body: List[str] = []

    def _walk(node: AgentConfig, is_main: bool) -> str:
        nid = _nid()
        role = _role(node, is_main)
        label = "<br/>".join(_node_lines(node, is_main, max_items=max_items))
        shape = _SHAPE_BY_ROLE.get(role, "rounded")
        # @{ shape } 与行内 :::class 在 mermaid.ink 上同时使用会触发 400,
        # 因此形状用 @{ shape } 声明, 配色用单独的 `class` 语句应用。
        body.append(f'{nid}["{label}"]@{{ shape: {shape} }}')
        body.append(f"class {nid} {role}")

        children = node.subagents or []
        for child in children:
            cid = _walk(child, False)
            body.append(_edge(nid, cid))

        # general-purpose 是框架为每个含子代理的节点自动注入的同级委派目标
        if show_general_purpose and children:
            gid = _nid()
            body.append(f'{gid}["general-purpose<br/>〔auto-injected〕"]@{{ shape: rounded }}')
            body.append(f"class {gid} ghost")
            body.append(_edge(nid, gid, dashed=True))

        return nid

    _walk(root, True)

    header = [
        _MERMAID_INIT,
        "flowchart TD",
        "%% 子代理委派拓扑 (logical delegation tree)",
        _MERMAID_STYLES,
    ]
    mermaid = "\n".join(header + body)
    ascii_lines = _ascii_tree(root, True, "", True)
    return mermaid, ascii_lines


# ======================== 对外入口 ========================
def build_delegation_mermaid(
    agent_config: AgentConfig,
    show_general_purpose: bool = False,
    max_items: int = 4,
) -> str:
    """纯内存: 由 AgentConfig 合成 mermaid 文本 (不写文件)。

    供 API 直接返回; 前端用 mermaid.js 渲染成图。零网络/图像依赖。
    """
    mermaid, _ascii = _build_mermaid(
        agent_config,
        show_general_purpose=show_general_purpose,
        max_items=max_items,
    )
    return mermaid


def render_delegation_tree(
    agent_config: AgentConfig,
    output_dir: str = "topology",
    output_name: str = "delegation_tree",
    draw_png: bool = True,
    show_general_purpose: bool = False,
    max_items: int = 4,
    edge_label: str = "",
) -> dict:
    """渲染合并的子代理委派拓扑到文件 (CLI/seed 用)。

    输出:
      - <output_name>.mmd : mermaid 文本 (始终生成, 不依赖网络)
      - <output_name>.png : 单张拓扑图 (draw_png=True 且渲染后端可用)
      - 控制台打印 ASCII 树预览

    Returns:
        {"mmd": path, "png": path|None, "tree": [ascii lines]}
    """
    os.makedirs(output_dir, exist_ok=True)
    mermaid, ascii_lines = _build_mermaid(
        agent_config,
        show_general_purpose=show_general_purpose,
        max_items=max_items,
        edge_label=edge_label,
    )

    mmd_path = os.path.join(output_dir, f"{output_name}.mmd")
    with open(mmd_path, "w", encoding="utf-8") as f:
        f.write(mermaid)

    png_path: Optional[str] = None
    if draw_png:
        try:
            png_bytes = draw_mermaid_png(mermaid_syntax=mermaid)
            png_path = os.path.join(output_dir, f"{output_name}.png")
            with open(png_path, "wb") as f:
                f.write(png_bytes)
        except Exception as e:
            logger.warning("委派拓扑 PNG 生成失败 (可能缺网络/渲染后端): %s", e)

    return {"mmd": mmd_path, "png": png_path, "tree": ascii_lines}
