"""image_understand 工具（plan W5/W6：ReAct 中途主动图像解析）。

链路：image_ref 双形态取字节（公网 URL：SSRF 守卫 + 流式大小上限 + 魔数
嗅探｜`/workspace/` 虚拟路径：越境前传 key、服务端解析单点 + containment
栅栏）→ 内容寻址落 MinIO（`runtime/{session}/{sha256}.{ext}`，同图去重、
与 shell 解耦）→ 视觉模型解析链（调用时刻现查：下挂 vision_model_id →
平台默认（model.is_default_vision）→ 基座 supports_vision；全无 = 结构化
退化信封，回合不断）→ 描述 + 1h 现签 image_url（照抄 knowledge_service
"存 ref、读取时签"模式，持久化签名 URL 属违例）。

实际 model_id 随调用日志落盘（会话/调用 → 模型成本归因）；base64 仅在
送视觉模型那一刻临时生成，永不入 LLM 上下文。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import logging
import re
import socket
import uuid as uuid_mod
from urllib.parse import urlparse

import filetype
import httpx
from fastmcp import FastMCP

from app.mcps.bind import mcps_state
from app.mcps.gd_ctx import GdCtx
from app.core.user_scoped_backend import user_workspace_path
from app.models import db as dbmod

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_WORKSPACE_PREFIX = "/workspace/"
# 会话段（thread_id）作 MinIO 前缀的防御性归一（生成侧本为 thread-<hex>）
_SESSION_KEY_RE = re.compile(r"[^A-Za-z0-9_-]")


def _err(message: str, ref: str | None = None) -> dict:
    out = {"result": "error", "message": message}
    if ref:
        out["ref"] = ref
    return out


def _assert_public_url(url: str) -> None:
    """运行时外链下载守卫（严于注册守卫）：内网/回环/保留地址一律拒绝。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 图像 URL: {parsed.scheme or '(空)'}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("图像 URL 缺少 host")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except Exception as e:  # noqa: BLE001 — 域名解析失败按不可达拒绝
        raise ValueError(f"无法解析图像 URL 主机: {host}") from e
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ValueError(f"图像 URL 指向内网/回环/保留地址被拒绝: {host}")


async def _download_image(url: str, max_bytes: int) -> bytes:
    """流式下载 + 硬上限（先拒超限 content-length，再拒读取超限）。"""
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as cli:
        async with cli.stream("GET", url) as resp:
            resp.raise_for_status()
            length = resp.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise ValueError(f"图像超过大小上限（{max_bytes // 1024 // 1024}MB）")
            chunks: list[bytes] = []
            read = 0
            async for chunk in resp.aiter_bytes():
                read += len(chunk)
                if read > max_bytes:
                    raise ValueError(f"图像超过大小上限（{max_bytes // 1024 // 1024}MB）")
                chunks.append(chunk)
            return b"".join(chunks)


async def _read_workspace_image(
    st, virtual_path: str, workspace_key: str | None, max_bytes: int
) -> bytes:
    """`/workspace/` 虚拟路径 → 字节（解析单点 + containment 栅栏）。

    契约：agent 按 fs 工具所见形态传虚拟绝对路径；越境前 wrapper 已把
    workspace **键**放进 _gd_ctx（contextvar 不跨 HTTP 边界），此处经
    user_workspace_path 取真实根（与写入后端/浏览 API 同源，写读一致），
    resolve() 后 containment 校验拒绝 `../` 越界与 symlink 外逃。
    """
    if not virtual_path.startswith(_WORKSPACE_PREFIX):
        raise ValueError(f"仅支持 {_WORKSPACE_PREFIX} 虚拟路径或 http(s) URL: {virtual_path}")
    if not workspace_key:
        raise ValueError("当前运行无工作区身份（workspace 能力未启用或图外调用）")

    def _read() -> bytes:
        base = user_workspace_path(st.settings.workspace_dir, workspace_key)
        target = (base / virtual_path[len(_WORKSPACE_PREFIX):]).resolve()
        if not target.is_relative_to(base):
            raise ValueError("工作区路径越界被拒绝")
        if not target.is_file():
            raise ValueError(f"工作区文件不存在: {virtual_path}")
        if target.stat().st_size > max_bytes:
            raise ValueError(f"图像超过大小上限（{max_bytes // 1024 // 1024}MB）")
        return target.read_bytes()

    return await asyncio.to_thread(_read)


def _resolve_vision_model(st, agent_row: dict | None) -> tuple[uuid_mod.UUID | None, str]:
    """解析链（现查绝不快照）：下挂 → 平台默认 → 基座。返回 (model_id, 来源)。"""
    mounted = agent_row.get("vision_model_id") if agent_row else None
    if mounted is not None and st.model_store.supports_vision(mounted):
        return mounted, "mounted"
    default_id = st.model_store.default_vision_model()
    if default_id is not None:
        return default_id, "default"
    base_id = agent_row.get("model_id") if agent_row else None
    if base_id is not None and st.model_store.supports_vision(base_id):
        return base_id, "base"
    return None, "none"


def register(mcp: FastMCP) -> None:
    """把图像理解工具注册到 FastMCP 实例上。"""

    @mcp.tool()
    async def image_understand(
        image_ref: str,
        prompt: str = "",
        _gd_ctx: GdCtx | None = None,
    ) -> dict:
        """主动分析一张图像，返回结构化描述与可展示的图片链接。

        适用：用户提问涉及图片（截图/图表/照片）、需要转写图中数据或界面
        元素、或需要追问图片细节时，在回答中途调用本工具。

        Args:
            image_ref: 图像定位。两种形态：公网图片 URL（https://...）直接
                传入；已落工作区的图片传 /workspace/ 虚拟路径（需复用或图
                过大时先用文件工具保存再传路径）。
            prompt: 针对性提问（如"转写图表全部数值"、"描述界面各按钮与
                报错文字"）。留空 = 通用全量描述。同图多问可分次调用。
        """
        st = mcps_state()
        if st.vision_describer is None:
            return _err("视觉描述器未装配")
        max_bytes = st.settings.attachment_max_size_mb * 1024 * 1024

        # ① 取字节（URL / workspace 虚拟路径）
        try:
            if image_ref.startswith(("http://", "https://")):
                raw = await _download_image(image_ref, max_bytes)
            else:
                raw = await _read_workspace_image(
                    st, image_ref, _gd_ctx.workspace if _gd_ctx else None, max_bytes
                )
        except ValueError as e:
            return _err(str(e))
        except FileNotFoundError as e:
            return _err(str(e))
        except Exception as e:  # noqa: BLE001 — 网络不可达 / IO 失败
            return _err(f"图像获取失败: {e}")

        # ② 魔数嗅探（服务端判定唯一可信源）
        guessed = filetype.guess(raw)
        if guessed is None or not (guessed.mime or "").startswith("image/"):
            return _err("内容不是可识别的图像（魔数校验失败）")

        # ③ 内容寻址落 MinIO（同图去重；会话前缀便于归档）
        sha = hashlib.sha256(raw).hexdigest()
        ext = guessed.extension or "png"
        session = (_gd_ctx.session_id if _gd_ctx and _gd_ctx.session_id else None) or "no-session"
        session_part = _SESSION_KEY_RE.sub("_", session)
        ref = f"runtime/{session_part}/{sha}.{ext}"
        try:
            if not await asyncio.to_thread(st.minio_storage.object_exists, ref):
                await asyncio.to_thread(
                    st.minio_storage.put_object, ref, raw, content_type=guessed.mime
                )
        except Exception as e:  # noqa: BLE001 — 存储故障不阻断描述主链路
            logger.warning("image_understand persist failed ref=%s: %s", ref, e)

        # ④ 解析链（agent 行现查；_gd_ctx 缺失时仅平台默认分支可达）
        agent_row = None
        if _gd_ctx and _gd_ctx.agent_id:
            try:
                agent_row = await dbmod.get_agent(st.pool, uuid_mod.UUID(_gd_ctx.agent_id))
            except ValueError:
                agent_row = None
        model_id, source = _resolve_vision_model(st, agent_row)
        if model_id is None:
            return _err(
                "当前智能体无可用视觉模型（下挂/平台默认/基座均未配置），"
                "请管理员在 /models 注册视觉模型或为智能体下挂",
                ref=ref,
            )

        # ⑤ 描述（base64 仅此处临时生成）+ 1h 现签渲染令牌
        data_url = f"data:{guessed.mime};base64,{base64.b64encode(raw).decode('ascii')}"
        try:
            description = await st.vision_describer.describe(
                model_id, data_url, prompt or None
            )
        except Exception as e:  # noqa: BLE001 — DescriberError 等统一退化
            return _err(f"图像描述失败: {e}", ref=ref)
        try:
            image_url = await asyncio.to_thread(st.minio_storage.presign_get, ref)
        except Exception:  # noqa: BLE001 — 签名失败仅失去渲染令牌
            image_url = None

        logger.info(
            "image_understand ok agent=%s model=%s source=%s ref=%s bytes=%d",
            _gd_ctx.agent_id if _gd_ctx else "-", model_id, source, ref, len(raw),
        )
        out = {
            "result": "ok",
            "description": description,
            "ref": ref,
            "model": str(model_id),
        }
        if image_url:
            out["image_url"] = image_url
            out["image_url_hint"] = "1 小时内有效的渲染链接；回答中须以 markdown ![](image_url) 内嵌展示"
        return out
