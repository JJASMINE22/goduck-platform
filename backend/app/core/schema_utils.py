"""Agent config schema + YAML/JSON loaders (migrated from DeepAgentsDemos).

The AgentConfig tree is the single source of truth frozen into agent.config_json.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class RubricConfig(BaseModel):
    """Per-agent-node rubric self-evaluation config.

    Mounted where it lives, it grades where it lives:
    - main gateway: grades final user-facing reply (incl. subagent tool messages).
    - subagent: self-evaluates inside its own graph; only handed back to parent once satisfied.
    """

    content: Optional[str] = Field(
        default=None,
        description="评分准则文本。为空则该节点不启用 rubric。",
    )
    fixed: bool = Field(
        default=False,
        description="True=常驻 rubric (FixedRubricMiddleware, 无显式 rubric 时注入); "
        "False=仅由调用方在 invoke payload 中传 rubric 时生效。",
    )
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=20,
        description="单次 rubric 尝试的最大评审轮数, 上限 20。",
    )
    grader_tool_names: List[str] = Field(
        default_factory=list,
        description="grader 可调用的取证工具名 (从 GRADER_TOOL_REGISTRY 解析), 与主代理工具集无关。",
    )


class AgentConfig(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    description: Optional[str] = Field(default=None, min_length=1)
    system_prompt: str = Field(..., min_length=1)
    tool_names: List[str] = Field(default_factory=list)
    skills: Optional[List[str]] = Field(default=None)
    memory: Optional[List[str]] = Field(
        default=None,
        description="memory 文件路径列表 (如 ['/memories/preferences.md'])。仅主代理生效; "
        "非空时挂载 /memories/ 后端路由并启用 MemoryMiddleware; "
        "为空或 None 时该层不启用 memory (主代理未声明则回落到默认偏好文件)。",
    )
    workspace: Optional[str] = Field(
        default=None,
        description="工作区目录名 (相对 LOCAL_ROOT, 如 'workspace'); 仅主代理生效。"
        "非空时挂载 /workspace/ 文件系统后端路由; "
        "为 None 或空字符串时不挂载该路由 (routes 不更新)。"
        "与 sandbox 解耦, 无沙箱亦可使用。",
    )
    interrupt_on: Optional[Dict[str, bool]] = Field(default=None)
    is_main: bool = False
    is_dicted: bool = False
    subagents: Optional[List[AgentConfig]] = None
    rubric: Optional[RubricConfig] = None
    allow_attachments: bool = Field(
        default=False,
        description="是否启用附件/多模态能力开关。模型多模态能力由 model registry 元数据驱动。",
    )

    @field_validator("skills")
    @classmethod
    def validate_skills(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for s in v:
            if not re.match(r"^/skills/[^/]+/$", s):
                raise ValueError(f"技能路径格式错误: '{s}', 期望格式 '/skills/<name>/'")
        return v

    @field_validator("memory")
    @classmethod
    def validate_memory(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for s in v:
            if not s.startswith("/memories/") or not s.endswith(".md"):
                raise ValueError(f"memory 路径格式错误: '{s}', 期望格式 '/memories/<name>.md'")
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            return v
        if os.sep in v or "/" in v or "\\" in v:
            raise ValueError(f"workspace 应为相对 LOCAL_ROOT 的单一目录名 (不含路径分隔符), 实际: '{v}'")
        return v

    @model_validator(mode="after")
    def validate_topology(self) -> AgentConfig:
        if self.is_main:
            if self.name is not None:
                raise ValueError("主代理(is_main=True)不需要设置 name")
            if self.description is not None:
                raise ValueError("主代理(is_main=True)不需要设置 description")
        else:
            if not self.name:
                raise ValueError("子代理必须设置 name")
            if not self.description:
                raise ValueError("子代理必须设置 description")
            if self.workspace:
                raise ValueError("子代理不能设置 workspace 工作区")
            if self.memory:
                raise ValueError("子代理不能设置 memory 偏好文件")

        if self.is_main and self.is_dicted:
            raise ValueError("主代理(is_main=True)不能为 dicted 模式")

        if self.is_dicted and self.subagents:
            raise ValueError(
                f"子代理 '{self.name}' 为 dicted 模式, 不允许嵌套 subagents"
            )

        return self

    @model_validator(mode="after")
    def validate_subagents(self) -> AgentConfig:
        if self.subagents:
            for sa in self.subagents:
                if sa.is_main:
                    raise ValueError("子代理不能设置 is_main=True")
                if sa.memory:
                    raise ValueError("子代理不能设置 memory 偏好文件")
                if sa.workspace:
                    raise ValueError("子代理不能设置 workspace 工作区")

        return self

    def to_dict(self) -> dict:
        result = {}
        for key, value in self:
            if value is None:
                continue
            if key == "subagents" and value:
                result[key] = [sa.to_dict() for sa in value]
            elif key == "rubric" and value is not None:
                result[key] = value.model_dump(exclude_none=True)
            else:
                result[key] = value
        return result


# ======================== Loaders ========================

def load_config_from_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"YAML 配置文件根节点必须是 dict, 实际类型: {type(data).__name__}")
    return data


def parse_config(config: dict) -> AgentConfig:
    """Validate a plain dict (JSON or YAML) into AgentConfig.

    100% reused by POST /topologies/validate (plan §11.3 — no rule rewrites).
    """
    return AgentConfig.model_validate(config)


def parse_yaml_config(path: str | Path) -> AgentConfig:
    return parse_config(load_config_from_yaml(path))


def config_to_yaml(agent_config: AgentConfig | dict) -> str:
    """Export config tree as YAML (for /topologies/{id}/export.yaml)."""
    data = agent_config.to_dict() if isinstance(agent_config, AgentConfig) else agent_config
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
