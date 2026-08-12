"""工作区浏览 API（plan §3.1-4）—— 按用户隔离的可视化浏览 + 预览。

所有接口均为 session 作用域：``user_id`` 取自 ``session.user_id``（非裸 query
参数），路径同源复用 ``UserScopedFilesystemBackend`` 的 sanitize（
``app/core/user_scoped_backend.py::user_workspace_path``），从结构上保证"写在哪
= 读在哪"——杜绝 backend 落 ``u_<hash>/`` 而浏览 API 查 ``<raw>/`` 的错配。

接口（router prefix /sessions）：
- GET    /sessions/{id}/workspace-capability : 是否启用工作区（驱动面板可见性）
- GET    /sessions/{id}/workspace/tree       : 递归文件树（受限条目/深度）
- GET    /sessions/{id}/workspace/file       : 读文本文件（带截断；二进制仅元信息）
- GET    /sessions/{id}/workspace/download   : 下载单文件（RFC5987 中文文件名）或目录 zip
- GET    /sessions/{id}/workspace/preview    : office 文档 markitdown → Markdown
- DELETE /sessions/{id}/workspace            : 删除文件或目录（用户裁剪拓扑，递归删目录）

安全（plan §4 缓解）：
- 路径穿越：``_resolve_safe`` 用 ``resolve().relative_to(user_root)`` 校验，跟随
  符号链接后必须仍在用户根内（与 FilesystemBackend._resolve_path 同构）。
- 体积上限：文本读取 / 下载 / 目录 zip 均设上限，超限返回 413。
- 删除：仅删用户工作区内文件/目录；禁止删用户根（空路径/`.`）。删除是显式用户
  行为，不影响智能体逻辑（产物落盘由 agent 控制，用户裁剪历史产物以专注当前任务）。
"""
from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.core.schema_utils import AgentConfig
from app.core.user_scoped_backend import user_workspace_path
from app.deps import get_appstate
from app.models import db as dbmod
from app.models.schemas import (
    WorkspaceCapability,
    WorkspaceDeleted,
    WorkspaceFileContent,
    WorkspaceNode,
    WorkspacePreview,
)
from app.services.doc_convert import CONVERTIBLE_EXTS, convert_to_markdown
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["工作区浏览"])

# 受限浏览：避免一次性遍历超大目录拖垮响应 / 内存。
MAX_TREE_ENTRIES = 500
MAX_TREE_DEPTH = 8
# 文本读取上限（256KB）：超出截断，提示下载查看完整内容。
MAX_TEXT_BYTES = 256 * 1024
# 单文件下载 / 目录 zip 总量上限（100MB）。
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _load_session_user(st, session_id: uuid.UUID) -> str:
    """加载 session 并返回 user_id（404 if not found）。"""
    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session["user_id"]


def _user_root(st, user_id: str) -> Path:
    """该用户的磁盘工作区根（同源 sanitize，写=读）。"""
    return user_workspace_path(st.settings.workspace_dir, user_id)


def _resolve_safe(user_root: Path, rel: str) -> Path:
    """把相对虚拟路径解析到用户根下，带穿越防护。

    resolve() 跟随符号链接；若解析后落在 user_root 之外（含 ``..`` 逃逸、软链
    指出），relative_to 抛 ValueError → 400。
    """
    rel = (rel or "").strip().lstrip("/")
    candidate = (user_root / rel).resolve() if rel else user_root.resolve()
    try:
        candidate.relative_to(user_root.resolve())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="非法路径") from e
    return candidate


def _stat_mtime(path: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# capability
# ---------------------------------------------------------------------------
@router.get("/{session_id}/workspace-capability", response_model=WorkspaceCapability)
async def workspace_capability(session_id: uuid.UUID, request: Request):
    """是否启用工作区 —— 驱动会话页工作区面板可见性（准则 F2 能力驱动 UI）。

    allow_workspace = 该会话智能体配置 workspace=true。
    """
    st = get_appstate(request)
    session = await SessionService(st.pool).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    allow = False
    agent_row = await dbmod.get_agent(st.pool, session["agent_id"])
    if agent_row is not None:
        allow = AgentConfig.model_validate(agent_row["config_json"]).workspace
    return WorkspaceCapability(allow_workspace=allow)


# ---------------------------------------------------------------------------
# tree
# ---------------------------------------------------------------------------
@router.get("/{session_id}/workspace/tree", response_model=list[WorkspaceNode])
async def workspace_tree(session_id: uuid.UUID, request: Request):
    """递归列出该用户工作区文件树（受限条目/深度）。空工作区返回 []。"""
    st = get_appstate(request)
    user_id = await _load_session_user(st, session_id)
    root = _user_root(st, user_id)
    if not root.is_dir():
        return []

    def build(dir_path: Path, depth: int, counter: dict) -> list[WorkspaceNode]:
        nodes: list[WorkspaceNode] = []
        if depth >= MAX_TREE_DEPTH:
            return nodes
        try:
            children = sorted(
                dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except (OSError, PermissionError):
            return nodes
        for child in children:
            if counter["n"] >= MAX_TREE_ENTRIES:
                break
            try:
                is_dir = child.is_dir()
                stat = child.stat()
                node = WorkspaceNode(
                    name=child.name,
                    path=child.relative_to(root).as_posix(),
                    is_dir=is_dir,
                    size=0 if is_dir else int(stat.st_size),
                    modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    children=None,
                )
                counter["n"] += 1
                if is_dir:
                    node.children = build(child, depth + 1, counter)
                nodes.append(node)
            except (OSError, PermissionError):
                continue
        return nodes

    return await asyncio.to_thread(build, root, 0, {"n": 0})


# ---------------------------------------------------------------------------
# file (text)
# ---------------------------------------------------------------------------
@router.get("/{session_id}/workspace/file", response_model=WorkspaceFileContent)
async def workspace_file(
    session_id: uuid.UUID,
    request: Request,
    path: str = Query(..., description="相对用户工作区根的路径"),
):
    """读文本文件内容（带截断）。二进制文件 content=None，前端改走 download/preview。"""
    st = get_appstate(request)
    user_id = await _load_session_user(st, session_id)
    root = _user_root(st, user_id)
    full = _resolve_safe(root, path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    size = full.stat().st_size
    mtime = _stat_mtime(full)

    def _read_text():
        with open(full, "rb") as fh:
            raw = fh.read(MAX_TEXT_BYTES + 1)
        truncated = len(raw) > MAX_TEXT_BYTES
        raw = raw[:MAX_TEXT_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None  # 二进制
        # NUL 字节是二进制内容的强信号（避免把含零字节的数据当文本渲染）。
        if "\x00" in text:
            return None
        return text, truncated

    result = await asyncio.to_thread(_read_text)
    if result is None:
        return WorkspaceFileContent(
            path=path, name=full.name, size=size, is_text=False, modified_at=mtime
        )
    text, truncated = result
    return WorkspaceFileContent(
        path=path,
        name=full.name,
        size=size,
        is_text=True,
        content=text,
        truncated=truncated,
        modified_at=mtime,
    )


# ---------------------------------------------------------------------------
# preview (office → markdown via markitdown)
# ---------------------------------------------------------------------------
@router.get("/{session_id}/workspace/preview", response_model=WorkspacePreview)
async def workspace_preview(
    session_id: uuid.UUID,
    request: Request,
    path: str = Query(..., description="相对用户工作区根的路径"),
):
    """office 文档（docx/xlsx/pptx/pdf 等）→ markitdown Markdown，供前端 marked 渲染。

    markitdown 未就绪或转换失败 → 503（前端回退降级卡片）。
    非 office 类型 → 415。
    """
    st = get_appstate(request)
    user_id = await _load_session_user(st, session_id)
    root = _user_root(st, user_id)
    full = _resolve_safe(root, path)
    if not full.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    ext = full.suffix.lower()
    if ext not in CONVERTIBLE_EXTS:
        raise HTTPException(
            status_code=415, detail=f"该类型({ext or '无扩展名'})不支持预览转换"
        )

    markdown = await asyncio.to_thread(convert_to_markdown, full)
    if markdown is None:
        raise HTTPException(
            status_code=503,
            detail="文档预览引擎未就绪（markitdown 未安装或转换失败），请改用下载",
        )
    return WorkspacePreview(path=path, markdown=markdown)


# ---------------------------------------------------------------------------
# download (single file RFC5987 filename | directory zip)
# ---------------------------------------------------------------------------
@router.get("/{session_id}/workspace/download")
async def workspace_download(
    session_id: uuid.UUID,
    request: Request,
    path: str = Query(..., description="相对用户工作区根的路径（文件或目录）"),
):
    """下载单文件（中文文件名走 RFC5987）或目录（流式 zip，总量受限）。"""
    st = get_appstate(request)
    user_id = await _load_session_user(st, session_id)
    root = _user_root(st, user_id)
    full = _resolve_safe(root, path)
    if not full.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    if full.is_file():
        if full.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件超过下载大小限制")
        media_type, _ = mimetypes.guess_type(full.name)
        # Starlette FileResponse 对非 ASCII filename 自动生成
        # Content-Disposition: ...; filename=<ascii>; filename*=UTF-8''<pct>
        # —— 中文文件名（如 第一次审查意见通知书.docx）不会乱码。
        return FileResponse(
            path=str(full), filename=full.name, media_type=media_type or "application/octet-stream"
        )

    # directory → 流式 zip（先核算总量，超限 413，避免内存爆）
    total = 0
    file_list: list[Path] = []
    for p in full.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            file_list.append(p)
            if total > MAX_DOWNLOAD_BYTES:
                raise HTTPException(status_code=413, detail="目录总量超过下载大小限制")

    arc_base = full.parent

    def _zip_chunks():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in file_list:
                # 以选中目录名作为 zip 根，保留相对结构。
                zf.write(p, p.relative_to(arc_base).as_posix())
        buf.seek(0)
        while True:
            chunk = buf.read(64 * 1024)
            if not chunk:
                break
            yield chunk

    zip_name = f"{full.name}.zip"
    return StreamingResponse(
        _zip_chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


# ---------------------------------------------------------------------------
# delete (file or directory; user prunes the topology)
# ---------------------------------------------------------------------------
@router.delete("/{session_id}/workspace", response_model=WorkspaceDeleted)
async def workspace_delete(
    session_id: uuid.UUID,
    request: Request,
    path: str = Query(..., description="相对用户工作区根的路径（文件或目录）"),
):
    """删除文件或目录（递归删目录）—— 用户裁剪历史产物以专注当前任务。

    纯用户行为：产物由 agent 落盘，删除不影响智能体逻辑（agent 写入按需重建）。
    安全：路径穿越由 ``_resolve_safe`` 拦截；禁止删除用户根（空路径 / "."）。
    """
    st = get_appstate(request)
    user_id = await _load_session_user(st, session_id)
    root = _user_root(st, user_id)

    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="请指定要删除的文件或目录")

    full = _resolve_safe(root, path)
    # 禁止删除用户根（resolve 后等于 root，含 "." 输入）。
    if full == root.resolve():
        raise HTTPException(status_code=400, detail="不能删除工作区根目录")
    if not full.exists():
        raise HTTPException(status_code=404, detail="文件或目录不存在")

    is_dir = full.is_dir()
    name = full.name

    def _remove():
        if is_dir:
            shutil.rmtree(full)
        else:
            full.unlink()

    try:
        await asyncio.to_thread(_remove)
    except OSError as e:
        strerror = getattr(e, "strerror", None) or str(e)
        raise HTTPException(status_code=500, detail=f"删除失败: {strerror}") from e

    logger.info("workspace delete: user=%s path=%s is_dir=%s", _sanitize_log(user_id), rel, is_dir)
    return WorkspaceDeleted(deleted=rel, name=name, was_dir=is_dir)


def _sanitize_log(user_id: str) -> str:
    """日志脱敏：不落原始 user_id（即便它已是白名单内值）。"""
    from app.core.user_scoped_backend import _sanitize_user

    return _sanitize_user(user_id)
