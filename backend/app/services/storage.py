"""MinIO 客户端封装（附件字节持久层）。

参照现有 pool 模式：AppState 持有单例，通过依赖注入暴露。
存 raw bytes（非 base64），base64 仅在送给在线 vision LLM 那一刻临时生成。
"""
from __future__ import annotations

import logging

from minio import Minio

from app.config import Settings

logger = logging.getLogger(__name__)


class MinioStorage:
    """MinIO 客户端封装，负责 bucket 初始化 + 对象读写。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket = settings.minio_bucket

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
            logger.info("MinIO bucket '%s' created", self.bucket)

    def put_object(self, bucket_key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        import io

        self.client.put_object(
            bucket_name=self.bucket,
            object_name=bucket_key,
            data=io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def get_object(self, bucket_key: str) -> bytes:
        with self.client.get_object(self.bucket, bucket_key) as resp:
            return resp.data

    def object_exists(self, bucket_key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, bucket_key)
            return True
        except Exception:
            return False

    def remove_object(self, bucket_key: str) -> None:
        try:
            self.client.remove_object(self.bucket, bucket_key)
        except Exception as e:
            logger.warning("删除 MinIO 对象失败 %s: %s", bucket_key, e)

    def remove_prefix(self, prefix: str) -> None:
        """删除某前缀下全部对象（尽力而为）：docling 分片边车
        （{bucket_key}.shards/）与 kb-src 归档（kb-src/{doc_id}/）的清理用。"""
        try:
            objects = self.client.list_objects(self.bucket, prefix=prefix, recursive=True)
            for obj in objects:
                self.client.remove_object(self.bucket, obj.object_name)
        except Exception as e:
            logger.warning("删除 MinIO 前缀对象失败 %s: %s", prefix, e)

    def presign_get(self, bucket_key: str, expires_seconds: int = 3600) -> str:
        """对象 GET 预签名 URL（doc_uri → 前端 PDF.js 按需拉取原始 PDF；
        HTTP Range 由 MinIO 原生支持，viewer 侧 disableAutoFetch 按页取流）。

        由 MINIO_RELATIVE_PRESIGN 开关决定形态：
        - false（默认，本地 dev）：SDK 原生绝对 URL——浏览器直连 MinIO（如
          localhost:9000），无代理依赖；
        - true（生产 compose）：同源相对路径 /minio/<bucket>/<key>?sig——浏览器
          经 nginx /minio/ 代理回源（代理保持签名时的 Host 头，SigV4 校验不受
          影响）。相对化三得：内网地址不外泄、任意访问域名（LAN / 反代域名）下
          均同源可用、天然过边缘 basic auth 门禁。"""
        from datetime import timedelta
        from urllib.parse import urlsplit

        url = self.client.presigned_get_object(
            self.bucket,
            bucket_key,
            expires=timedelta(seconds=max(60, expires_seconds)),
        )
        if not getattr(self.settings, "minio_relative_presign", False):
            return url
        parts = urlsplit(url)
        return f"/minio{parts.path}?{parts.query}"


def build_minio_storage(settings: Settings) -> MinioStorage:
    storage = MinioStorage(settings)
    storage.ensure_bucket()
    return storage
