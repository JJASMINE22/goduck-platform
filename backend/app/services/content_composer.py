"""ContentComposer：内容组装器（plan §六 核心新增）。

把「组装 HumanMessage.content」从 stream.py 提取为独立类，职责单一、可测试、
可注入。只产出「发送体」（content: str | list），不执行流式（流式仍由
stream_adapter.stream_message 负责）。
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from psycopg_pool import AsyncConnectionPool

from app.models import db as dbmod
from app.services.storage import MinioStorage

logger = logging.getLogger(__name__)


class ContentComposer:
    """组装发往 agent 的 HumanMessage.content。

    输入: 用户 query + 附件 upload_ids + 视觉能力位
    输出: str (纯文本) 或 list (多模态结构)
    依赖: DB pool (查附件元数据), MinIO client (取图像字节)
    不依赖: model_registry (supports_vision 由调用方解析后传入)
    """

    def __init__(self, pool: AsyncConnectionPool, minio: MinioStorage):
        self.pool = pool
        self.minio = minio

    async def build(
        self,
        user_query: str,
        upload_ids: list[str],
        supports_vision: bool,
    ) -> str | list:
        if not upload_ids:
            return user_query

        # ① 查附件元数据 (kind/mime/size/preview 已在上传时存好, 这里只读)
        attachments = await self._load_attachments(upload_ids)

        # ② 路由分流 (kind × supports_vision)
        direct_images: list[dict] = []  # 直送 base64
        anchored: list[dict] = []  # 锚点注入
        for a in attachments:
            if a["kind"] == "image" and supports_vision:
                img_part = await self._image_to_b64(a)
                if img_part is not None:
                    direct_images.append(img_part)
                    continue
            anchored.append(a)  # 图片(非多模态或取字节失败) 或 文档

        # ③ 拼装最终 content
        anchor_text = self._build_anchor_text(user_query, anchored)
        if not direct_images:
            return anchor_text
        parts: list[dict[str, Any]] = [{"type": "text", "text": anchor_text}]
        parts.extend(direct_images)
        return parts

    async def _load_attachments(self, upload_ids: list[str]) -> list[dict]:
        """批量查附件元数据，按入参顺序返回。"""
        import uuid as _uuid

        rows: list[dict] = []
        for uid in upload_ids:
            try:
                row = await dbmod.get_upload(self.pool, _uuid.UUID(uid))
            except ValueError:
                row = None
            except Exception:
                row = None
            if row is not None:
                rows.append(row)
        return rows

    async def _image_to_b64(self, attachment: dict) -> dict | None:
        """取图像字节并转为 base64 content block。失败返回 None。"""
        try:
            raw = await self._get_bytes_async(attachment["bucket_key"])
        except Exception as e:
            logger.warning("取图像字节失败 %s: %s", attachment["id"], e)
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        mime = attachment.get("mime", "image/png")
        data_url = f"data:{mime};base64,{b64}"
        return {"type": "image_url", "image_url": {"url": data_url}}

    async def _get_bytes_async(self, bucket_key: str) -> bytes:
        """MinIO 客户端是同步的，用 asyncio 放线程池执行避免阻塞事件循环。"""
        import asyncio

        return await asyncio.to_thread(self.minio.get_object, bucket_key)

    def _build_anchor_text(self, user_question: str, attachments: list[dict]) -> str:
        if not attachments:
            return user_question

        rows = []
        for a in attachments:
            uid = str(a["id"])
            if a["kind"] == "image":
                rows.append(
                    f"| 图片 | {a['filename']} | {uid} | ({a['mime']}, {a['size']}B) |"
                )
            else:  # docx
                preview = (a.get("preview") or "")[:200]
                page_count = a.get("page_count")
                rows.append(
                    f"| 文档 | {a['filename']} | {uid} | (Word, {page_count}段, 首段: '{preview}') |"
                )

        table = "\n".join(rows)
        return f"""## 用户上传的附件

本次消息附带以下附件，供回答时按需调用对应工具：

| 类型 | 文件名 | upload_id | 备注 |
|------|--------|-----------|------|
{table}

**使用原则**：
- 仅当回答用户问题需要时，调用对应工具获取内容
- 必须使用上表中列出的真实 upload_id 调用工具，禁止自行编造或猜测 id
- 表中没有的附件不存在，不要调用工具获取不存在的图片/文档
- 无关附件不要调用，直接依据自身能力回答
- 未获取到内容时，如实说明，不得编造

---

用户问题：{user_question}"""
