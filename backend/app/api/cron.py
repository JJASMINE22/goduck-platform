"""Cron job management API.

接口面（对标 /sessions、/agents 定位）：
- CRUD + enable/disable（PUT 部分更新）+ 立即触发（POST /run，测试/补跑）
- 运行历史（GET /runs，最近 50 条）
- 创建/更新即落库即生效（DB 为唯一真相源，scheduler 下个 tick 内感知，
  响应体回带 next_run_at 供前端展示"下次运行时间"）。

错误契约（镜像 agent/session 模式）：
- KeyError → 404（agent / job 不存在）
- FileExistsError → 409（任务名/会话标题冲突）
- PermissionError → 409（绑定 allow_shell 智能体被拒：无人值守无审批通道）
- ValueError → 422（业务校验：间隔边界、agent 归档等）
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.deps import get_appstate, get_current_user
from app.models.schemas import CronJobCreate, CronJobOut, CronJobUpdate, CronRunOut
from app.services.cron_service import CronService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cron", tags=["定时任务"])


def _service(request: Request) -> CronService:
    st = get_appstate(request)
    return CronService(st.pool, st.settings)


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, KeyError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, FileExistsError):
        return HTTPException(status_code=409, detail=f"任务名 '{e.args[0]}' 已存在（同用户内唯一）")
    if isinstance(e, PermissionError):
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=422, detail=str(e))


async def _owned_job_or_404(svc: CronService, job_id: uuid.UUID, user: dict) -> dict:
    """属主校验：非本人任务一律 404（不暴露存在性）。"""
    job = await svc.get(job_id)
    if job is None or job["user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="cron job not found")
    return job


@router.post("/jobs", response_model=CronJobOut, status_code=status.HTTP_201_CREATED)
async def create_cron_job(
    body: CronJobCreate, request: Request, user: dict = Depends(get_current_user)
):
    """创建定时任务：校验 → 原子落库（专用会话 + 任务行）→ 201。

    落库即注册：scheduler 下个 tick 内感知，`next_run_at` 即首次运行时间。
    """
    svc = _service(request)
    try:
        job = await svc.create(
            user_id=user["id"],
            agent_id=body.agent_id,
            name=body.name,
            prompt=body.prompt,
            interval_minutes=body.interval_minutes,
            start_at=body.start_at,
        )
    except (KeyError, FileExistsError, ValueError, PermissionError) as e:
        raise _http_error(e)
    return await svc.get(job["id"])


@router.get("/jobs", response_model=list[CronJobOut])
async def list_cron_jobs(
    request: Request = None,
    user: dict = Depends(get_current_user),
):
    return await _service(request).list_by_user(user["id"])


@router.get("/jobs/{job_id}", response_model=CronJobOut)
async def get_cron_job(
    job_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    return await _owned_job_or_404(_service(request), job_id, user)


@router.put("/jobs/{job_id}", response_model=CronJobOut)
async def update_cron_job(
    job_id: uuid.UUID,
    body: CronJobUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """部分更新：改名/改 prompt/改调度/启停。

    调度规则（详见 CronJobUpdate docstring）：显式 start_at=该时刻首跑；
    仅改 interval 或重新启用=一个完整间隔后首跑。
    """
    svc = _service(request)
    await _owned_job_or_404(svc, job_id, user)
    provided = body.model_fields_set
    try:
        job = await svc.update(
            job_id,
            name=body.name,
            prompt=body.prompt,
            interval_minutes=body.interval_minutes,
            start_at=body.start_at,
            enabled=body.enabled,
            name_provided="name" in provided and body.name is not None,
            interval_provided="interval_minutes" in provided,
            start_at_provided="start_at" in provided,
        )
    except (KeyError, FileExistsError, ValueError, PermissionError) as e:
        raise _http_error(e)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")
    return job


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cron_job(
    job_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    """删除任务（级联删 run 历史）；专用会话软归档保留，历史产出仍可浏览。"""
    svc = _service(request)
    await _owned_job_or_404(svc, job_id, user)
    deleted = await svc.delete(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="cron job not found")
    return None


@router.post("/jobs/{job_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_cron_job_now(
    job_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    """立即触发一次（不推进 next_run_at）。禁用态任务也可手动触发（补跑/验证）。"""
    st = get_appstate(request)
    svc = _service(request)
    job = await _owned_job_or_404(svc, job_id, user)
    if st.cron_scheduler is None:
        raise HTTPException(status_code=503, detail="cron 调度器未启用（CRON_ENABLED=false）")
    ok, reason = await st.cron_scheduler.run_now(job)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    return {"status": "started"}


@router.get("/jobs/{job_id}/runs", response_model=list[CronRunOut])
async def list_cron_runs(
    job_id: uuid.UUID, request: Request, user: dict = Depends(get_current_user)
):
    await _owned_job_or_404(_service(request), job_id, user)
    return await _service(request).list_runs(job_id)
