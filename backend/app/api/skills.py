"""Skills upload / list / delete (plan §6).

Skills pool: {LOCAL_ROOT}/skills/ — one skill = one subdirectory.
AgentConfig.skills references skill names as /skills/<name>/.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form

from app.deps import get_appstate
from app.models.schemas import SkillInfo

router = APIRouter(prefix="/skills", tags=["技能管理"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _pool_dir(request: Request):
    return get_appstate(request).settings.skills_pool_dir


@router.get("", response_model=list[SkillInfo])
async def list_skills(request: Request):
    pool = _pool_dir(request)
    out: list[SkillInfo] = []
    for d in sorted(pool.iterdir()):
        if d.is_dir():
            files = [f for f in d.iterdir() if f.is_file()]
            out.append(SkillInfo(name=d.name, file_count=len(files)))
    return out


@router.post("", response_model=SkillInfo)
async def upload_skill(
    request: Request,
    name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="skill 名称仅允许字母/数字/下划线/连字符")
    pool = _pool_dir(request)
    skill_dir = pool / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        # basename only — prevent path traversal
        safe = (skill_dir / f.filename).resolve()
        if not str(safe).startswith(str(skill_dir.resolve())):
            raise HTTPException(status_code=400, detail=f"非法文件名: {f.filename}")
        content = await f.read()
        if not content.strip():
            continue  # skip empty files
        safe.write_bytes(content)
    saved = [f for f in skill_dir.iterdir() if f.is_file()]
    if not saved:
        raise HTTPException(status_code=400, detail="技能目录不能为空 (无有效文件)")
    return SkillInfo(name=name, file_count=len(saved))


@router.delete("/{name}")
async def delete_skill(name: str, request: Request):
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="skill 名称非法")
    pool = _pool_dir(request)
    skill_dir = pool / name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    for f in skill_dir.iterdir():
        if f.is_file():
            f.unlink()
    skill_dir.rmdir()
    return {"ok": True}
