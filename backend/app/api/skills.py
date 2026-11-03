"""Skills upload / list / delete (plan §6).

Skills pool: {LOCAL_ROOT}/skills/ — one skill = one subdirectory.
AgentConfig.skills references skill names as /skills/<name>/.

上传支持目录结构：前端将文件夹递归展开，按相对路径（如 scripts/run.js）作为
filename 上传；服务端在 <name>/ 下重建子目录并防目录穿越。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form

from app.deps import get_appstate, require_manager
from app.models.schemas import SkillDetail, SkillInfo

router = APIRouter(prefix="/skills", tags=["技能管理"])

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# 描述文件优先级：SKILL.md（frontmatter description）→ README.md
_MD_CANDIDATES = ("SKILL.md", "README.md")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def _frontmatter_description(md_path: Path) -> str | None:
    """读取 markdown YAML frontmatter 的 description 字段；缺失/解析失败返回 None。"""
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    desc = meta.get("description") if isinstance(meta, dict) else None
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    return None


def _pool_dir(request: Request):
    return get_appstate(request).settings.skills_pool_dir


@router.get("", response_model=list[SkillInfo])
async def list_skills(request: Request):
    pool = _pool_dir(request)
    out: list[SkillInfo] = []
    for d in sorted(pool.iterdir()):
        if d.is_dir():
            files = [f for f in d.rglob("*") if f.is_file()]
            out.append(SkillInfo(name=d.name, file_count=len(files)))
    return out


@router.post("", response_model=SkillInfo)
async def upload_skill(
    request: Request,
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    _admin: dict = Depends(require_manager),
):
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="skill 名称仅允许字母/数字/下划线/连字符")
    pool = _pool_dir(request)
    skill_dir = pool / name
    skill_root = skill_dir.resolve()
    skill_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        # 统一为 POSIX 相对路径，允许子目录结构（scripts/run.js）
        rel = (f.filename or "").replace("\\", "/").lstrip("/")
        if not rel:
            continue
        safe = (skill_dir / rel).resolve()
        # 防目录穿越：解析后必须落在 skill 目录内
        try:
            safe.relative_to(skill_root)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"非法文件名: {f.filename}")
        content = await f.read()
        if not content.strip():
            continue  # 跳过空文件
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_bytes(content)
    saved = [f for f in skill_dir.rglob("*") if f.is_file()]
    if not saved:
        raise HTTPException(status_code=400, detail="技能目录不能为空 (无有效文件)")
    return SkillInfo(name=name, file_count=len(saved))


@router.get("/{name}/description", response_model=SkillDetail)
async def get_skill_description(name: str, request: Request):
    """取技能描述：SKILL.md（缺失时回退 README.md）frontmatter 的 description。"""
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="skill 名称非法")
    pool = _pool_dir(request)
    skill_dir = pool / name
    if not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    desc: str | None = None
    for fname in _MD_CANDIDATES:
        md = skill_dir / fname
        if md.is_file():
            desc = _frontmatter_description(md)
            if desc:
                break
    return SkillDetail(name=name, description=desc)


@router.delete("/{name}")
async def delete_skill(
    name: str, request: Request, _admin: dict = Depends(require_manager)
):
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="skill 名称非法")
    pool = _pool_dir(request)
    skill_dir = pool / name
    if not skill_dir.exists() or not skill_dir.is_dir():
        raise HTTPException(status_code=404, detail="skill not found")
    shutil.rmtree(skill_dir)
    return {"ok": True}
