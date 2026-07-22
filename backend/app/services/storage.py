"""MinIO 客户端封装（附件字节持久层）。

参照现有 pool 模式：AppState 持有单例，通过依赖注入暴露。
存 raw bytes（非 base64），base64 仅在送给在线 vision LLM 那一刻临时生成。
"""
from __future__ import annotations

import logging
from typing import Optional

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


def build_minio_storage(settings: Settings) -> MinioStorage:
    storage = MinioStorage(settings)
    storage.ensure_bucket()
    return storage
