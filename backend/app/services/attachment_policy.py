"""AttachmentPolicyService：附件能力策略单一真相（plan §3.1）。

能力谓词（每次调用动态派生，绝不落 session 表快照——模型能力是管理员
随时可变的运维态，快照即第二真相）：

    allow_image = supports_vision(基座) OR 下挂视觉模型当前可用
    allow_file  = mime_utils 白名单派生（消灭各处硬编码 ['docx','pdf']）

运行时优先级：基座支持视觉 → 直传字节并休眠忽略 vision_model_id（覆盖
保存后基座模型能力翻转的漂移场景）；基座无视觉 + 已下挂 → 经下挂模型
预消费描述；均无 → 上传门 422。

消费方（三门 + 语音共享层）：
- GET  /sessions/{id}/attachment-capability（前端上传组件可见性）
- POST /sessions/{id}/uploads（上传门 422）
- POST /sessions/{id}/messages + voice WS（发送门，经 message_content）

会话-智能体解耦（plan 1790660700000 §5）：策略按**当回合 agent_id** 派生
（调用方解析后传入，缺省回落链在 API 层），不再读 session 表。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.core.schema_utils import AgentConfig
from app.models import db as dbmod
from app.services.mime_utils import allowed_file_extensions

if TYPE_CHECKING:
    from app.deps import AppState


@dataclass(frozen=True)
class AttachmentPolicy:
    """一次会话解析出的附件能力策略（三门共享同一结构）。"""

    allow_attachments: bool
    allow_image: bool
    allow_file: list[str]
    max_size_mb: int
    # 路由详情（组装层用；门控层只消费布尔位）
    base_supports_vision: bool
    vision_model_id: Optional[uuid.UUID]  # 仅当"基座无视觉且下挂当前可用"时非空


class AttachmentPolicyService:
    """agent_id → agent → model 能力解析（委托 ModelStore 内存缓存，不自查 DB model 表）。"""

    def __init__(self, st: "AppState"):
        self._st = st

    async def for_agent_id(self, agent_id: Optional[uuid.UUID]) -> AttachmentPolicy:
        """按当回合智能体派生能力（现查 agent 行，绝不快照）。

        agent_id 为 None（未选择智能体）或行不存在（悬空 last / 已删除）→
        "全关"降级而非 500：上传门据此 422、发送门据此拦截，能力端点据此
        隐藏上传组件——脏数据与空会话共用同一条优雅降级路径。
        """
        agent_row = (
            await dbmod.get_agent(self._st.pool, agent_id) if agent_id is not None else None
        )
        return self.for_agent_row(agent_row)

    def for_agent_row(self, agent_row: Optional[dict]) -> AttachmentPolicy:
        """行级纯派生（调用方自行解析行时的同步入口，见 uploads.py 能力端点）。"""
        st = self._st
        if agent_row is None:
            return AttachmentPolicy(
                allow_attachments=False,
                allow_image=False,
                allow_file=allowed_file_extensions(),
                max_size_mb=st.settings.attachment_max_size_mb,
                base_supports_vision=False,
                vision_model_id=None,
            )

        agent_cfg = AgentConfig.model_validate(agent_row["config_json"])
        base_supports_vision = st.model_store.supports_vision(agent_row["model_id"])

        # 下挂可用性 = 已注册（enabled 模型才进 ModelStore）且 supports_vision；
        # 被禁用/删除的模型经 refresh 自愈为"不可用"，谓词实时收紧。
        vision_mount: Optional[uuid.UUID] = None
        mounted = agent_row.get("vision_model_id")
        if (
            not base_supports_vision
            and mounted is not None
            and st.model_store.supports_vision(mounted)
        ):
            vision_mount = mounted

        return AttachmentPolicy(
            allow_attachments=agent_cfg.allow_attachments,
            allow_image=base_supports_vision or vision_mount is not None,
            allow_file=allowed_file_extensions(),
            max_size_mb=st.settings.attachment_max_size_mb,
            base_supports_vision=base_supports_vision,
            vision_model_id=vision_mount,
        )
