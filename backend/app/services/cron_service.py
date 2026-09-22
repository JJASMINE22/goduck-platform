"""Cron job CRUD + schedule semantics.

职责边界（与设计讨论对齐）：
- 校验：agent 存在且 active（404/409）、任务名同用户唯一（409）、间隔业务边界（422）。
- 落库即注册：无任何"向 scheduler 注册"的动作——DB 是唯一真相源，scheduler
  每 tick 轮询 due 行，创建/更新在下一个 tick（≤cron_tick_seconds）内自然生效。

调度语义（三条规则，前后端共同契约）：
1. create：next_run_at = max(start_at, now) —— 显式开始时间=该时刻首跑；
   立即开始（start_at 缺省/过去）= 下个 tick 首跑。interval 相位锚定 start_at。
2. 启用/重新启用（enabled false→true）：next_run_at = now + interval —— 无显式
   意图时保守等待一个完整间隔，避免反复开关立即烧钱；同时复活专用会话、
   复位熔断计数。
3. 调度字段变更（interval/start_at 显式提供且 enabled）：显式 start_at 按规则 1，
   否则按规则 2 重锚。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.core.schema_utils import cron_unattended_violations, tree_allows_shell
from app.models import db as dbmod

logger = logging.getLogger(__name__)

_CRON_SHELL_REFUSAL = (
    "定时任务不允许绑定开启 shell 能力的智能体（无人值守无审批通道）"
)

_CRON_UNATTENDED_REFUSAL = (
    "定时任务不允许绑定开启工具审批/附件/知识库能力的智能体"
    "（无人值守无审批通道，附件/知识库暂不支持）"
)


def normalize_start_at(start_at: Optional[datetime], now: datetime) -> datetime:
    """None→now；naive→视为 UTC；过去→夹到 now（首跑不早于当前时刻）。

    纯函数（便于单测）：datetime 均为 aware UTC 往返。
    """
    if start_at is None:
        return now
    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    return max(start_at.astimezone(timezone.utc), now)


def validate_interval_minutes(value: int, settings: Settings) -> int:
    """间隔业务边界（pydantic 只保证 ≥1，下限/上限由配置驱动）。"""
    lo, hi = settings.cron_min_interval_minutes, settings.cron_max_interval_minutes
    if not (lo <= value <= hi):
        raise ValueError(f"触发间隔须在 {lo}~{hi} 分钟之间（当前 {value}）")
    return value


class CronService:
    """任务管理服务：创建/更新/删除/查询 + 手动触发前置校验。"""

    def __init__(self, pool: AsyncConnectionPool, settings: Settings):
        self.pool = pool
        self.settings = settings

    # ------------------------------------------------------------------ read

    async def get(self, job_id: uuid.UUID) -> Optional[dict]:
        return await dbmod.get_cron_job(self.pool, job_id)

    async def list_by_user(self, user_id: str) -> list[dict]:
        return await dbmod.list_cron_jobs_by_user(self.pool, user_id)

    async def list_runs(self, job_id: uuid.UUID, limit: int = 50) -> list[dict]:
        return await dbmod.list_cron_runs(self.pool, job_id, limit)

    # ---------------------------------------------------------------- create

    async def create(
        self,
        *,
        user_id: str,
        agent_id: uuid.UUID,
        name: str,
        prompt: str,
        interval_minutes: int,
        start_at: Optional[datetime],
    ) -> dict:
        if not name:
            raise ValueError("任务名不能为空")
        if not prompt:
            raise ValueError("任务内容不能为空")
        validate_interval_minutes(interval_minutes, self.settings)

        agent_row = await dbmod.get_agent(self.pool, agent_id)
        if agent_row is None:
            raise KeyError(f"agent {agent_id} not found")
        if agent_row["status"] == "archived":
            raise ValueError(f"智能体 '{agent_row['name']}' 已归档，不能绑定定时任务")
        # kind 隔离（方案 A）：仅 cron 型智能体可绑定定时任务
        if agent_row.get("kind") != "cron":
            raise ValueError(
                f"智能体 '{agent_row['name']}' 为聊天会话类型（kind=chat），"
                "定时任务须绑定 cron 型智能体（创建智能体时选定，不可变）"
            )
        # shell 禁令（绑定门以能力为准、不以声明为准——递归整树复查，覆盖
        # usage=chat 配置与 YAML 直写路径；PermissionError → API 409）
        if tree_allows_shell(agent_row.get("config_json") or {}):
            raise PermissionError(
                f"智能体 '{agent_row['name']}' 的配置树含 allow_shell 节点。{_CRON_SHELL_REFUSAL}"
            )
        # 无人值守禁用能力（审批/附件/知识库）同款整树复查（validator 为第一道门，
        # 此处覆盖先绑定后改配置的时间差与直写 DB 旁路）
        violations = cron_unattended_violations(agent_row.get("config_json") or {})
        if violations:
            raise PermissionError(
                f"智能体 '{agent_row['name']}' 的配置树含 "
                f"{'、'.join(violations)}。{_CRON_UNATTENDED_REFUSAL}"
            )

        # 同用户任务名唯一预检（竞态兜底在 db 层 UniqueViolation → FileExistsError）
        if await dbmod.get_cron_job_by_user_name(self.pool, user_id, name) is not None:
            raise FileExistsError(name)

        now = datetime.now(timezone.utc)
        resolved_start = normalize_start_at(start_at, now)
        job = await dbmod.create_cron_job(
            self.pool,
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            prompt=prompt,
            interval_minutes=interval_minutes,
            start_at=resolved_start,
            next_run_at=resolved_start,
        )
        logger.info(
            "cron job '%s' created (user=%s agent=%s interval=%dm next_run_at=%s)",
            name, user_id, agent_id, interval_minutes, resolved_start.isoformat(),
        )
        return job

    # ---------------------------------------------------------------- update

    async def update(
        self,
        job_id: uuid.UUID,
        *,
        name: Optional[str] = None,
        prompt: Optional[str] = None,
        interval_minutes: Optional[int] = None,
        start_at: Optional[datetime] = None,
        enabled: Optional[bool] = None,
        start_at_provided: bool = False,
        interval_provided: bool = False,
        name_provided: bool = False,
    ) -> Optional[dict]:
        """部分更新。*_provided 显式区分"未提供"与"提供了 None"（start_at 语义
        不同：未提供=不动调度；提供（含 None）=以 now/给定值重锚）。
        """
        job = await dbmod.get_cron_job(self.pool, job_id)
        if job is None:
            return None

        now = datetime.now(timezone.utc)
        fields: dict = {}

        if name_provided and name is not None and name != job["name"]:
            if await dbmod.get_cron_job_by_user_name(self.pool, job["user_id"], name) is not None:
                raise FileExistsError(name)
            fields["name"] = name
        if prompt is not None:
            fields["prompt"] = prompt
        if interval_provided and interval_minutes is not None:
            validate_interval_minutes(interval_minutes, self.settings)

        schedule_changed = (interval_provided and interval_minutes is not None) or start_at_provided

        if enabled is not None:
            fields["enabled"] = enabled

        if enabled is True and not job["enabled"]:
            # 重新启用：须 agent 仍 active；复活会话；保守等一个完整间隔；复位熔断。
            await self._assert_agent_active(job["agent_id"])
            interval = interval_minutes if (interval_provided and interval_minutes) else job["interval_minutes"]
            await dbmod.reactivate_cron_session(self.pool, job["session_id"])
            fields["next_run_at"] = now + timedelta(minutes=interval)
            fields["consecutive_failures"] = 0
            fields["last_error"] = None
        elif schedule_changed and (enabled is None or enabled) and job["enabled"]:
            # 启用态下改调度：显式 start_at 按其时刻首跑，否则一个新间隔后首跑。
            interval = interval_minutes if (interval_provided and interval_minutes) else job["interval_minutes"]
            if start_at_provided:
                fields["next_run_at"] = normalize_start_at(start_at, now)
            else:
                fields["next_run_at"] = now + timedelta(minutes=interval)
        elif enabled is False and job["enabled"]:
            # 停用：槽位清空（last_* 保留作状态回显）。
            fields["next_run_at"] = None

        if interval_provided and interval_minutes is not None:
            fields["interval_minutes"] = interval_minutes
        if start_at_provided:
            fields["start_at"] = normalize_start_at(start_at, now)

        updated = await dbmod.update_cron_job(self.pool, job_id, fields)

        # 任务改名同步专用会话标题（撞车时保留旧标题，不影响任务本身）
        if name_provided and name is not None and "name" in fields:
            await self._rename_session_title(job["session_id"], name)
        return updated

    # ---------------------------------------------------------------- delete

    async def delete(self, job_id: uuid.UUID) -> bool:
        return await dbmod.delete_cron_job(self.pool, job_id)

    # --------------------------------------------------------------- helpers

    async def _assert_agent_active(self, agent_id: uuid.UUID) -> None:
        agent_row = await dbmod.get_agent(self.pool, agent_id)
        if agent_row is None:
            raise KeyError(f"agent {agent_id} not found")
        if agent_row["status"] == "archived":
            raise ValueError(f"智能体 '{agent_row['name']}' 已归档，不能启用/触发定时任务")
        if agent_row.get("kind") != "cron":
            raise ValueError("绑定的智能体非定时任务类型（kind!=cron），请换绑后再启用")
        # 时间差防御：先建任务、后改 agent 开 shell —— 重新启用时复查配置树
        if tree_allows_shell(agent_row.get("config_json") or {}):
            raise PermissionError(
                f"智能体 '{agent_row['name']}' 的配置树含 allow_shell 节点。{_CRON_SHELL_REFUSAL}"
            )
        # 时间差防御（同上）：审批/附件/知识库禁用能力复查
        violations = cron_unattended_violations(agent_row.get("config_json") or {})
        if violations:
            raise PermissionError(
                f"智能体 '{agent_row['name']}' 的配置树含 "
                f"{'、'.join(violations)}。{_CRON_UNATTENDED_REFUSAL}"
            )

    async def _rename_session_title(self, session_id: uuid.UUID, name: str) -> None:
        """专用会话标题跟随任务名。标题唯一索引撞车（用户手建了同名会话）时
        静默保留旧标题——会话仍可靠 kind/kind 徽标识别，改名不被此边缘阻塞。"""
        from psycopg.errors import UniqueViolation

        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "UPDATE session SET title = %s, updated_at = now() "
                        "WHERE id = %s AND kind = 'cron' AND status = 'active'",
                        (f"[定时] {name}", session_id),
                    )
                except UniqueViolation:
                    logger.warning(
                        "cron session %s title rename collided; keeping old title", session_id
                    )
