"""原始文件存储抽象：本地磁盘（默认）与 S3 兼容对象存储。

key 布局沿用现有相对路径 `{tenant_id}/{kb_id}/{doc_id}{suffix}`，
因此 `documents.source_url` 语义不变，已有本地数据无需迁移。
`STORAGE_BACKEND=local` 时不读取任何 S3 配置。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_UPLOAD_ROOT = Path("data/uploads")


class StorageError(RuntimeError):
    """存储后端不可用或返回错误；调用方按可重试失败处理。"""


class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    """本地磁盘实现；写入先落临时文件再 replace，保留原有的原子性。"""

    def __init__(self, root: str | Path = DEFAULT_UPLOAD_ROOT) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (self.root / key).resolve()
        if path != root and root not in path.parents:
            raise StorageError(f"非法存储 key：{key}")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    """boto3 实现；MinIO 必须用 path-style 寻址。"""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any = None,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        use_ssl: bool = False,
    ) -> None:
        self.bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region,
            use_ssl=use_ssl,
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"}
            ),
        )

    def put_bytes(self, key: str, data: bytes) -> None:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"S3 写入失败：{key}：{exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise StorageError(f"S3 读取失败：{key}：{exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 读取失败：{key}：{exc}") from exc
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise StorageError(f"S3 探测失败：{key}：{exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 探测失败：{key}：{exc}") from exc
        return True

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"S3 删除失败：{key}：{exc}") from exc


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"STORAGE_BACKEND=s3 时必须配置 {name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def build_storage() -> Storage:
    """按 `STORAGE_BACKEND` 选择实现；未知值直接报错，避免静默降级。"""
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    if backend == "local":
        return LocalStorage(DEFAULT_UPLOAD_ROOT)
    if backend == "s3":
        return S3Storage(
            endpoint_url=os.environ.get("S3_ENDPOINT", ""),
            bucket=_required_env("S3_BUCKET"),
            access_key=os.environ.get("S3_ACCESS_KEY", ""),
            secret_key=os.environ.get("S3_SECRET_KEY", ""),
            region=os.environ.get("S3_REGION", "us-east-1"),
            use_ssl=_env_bool("S3_USE_SSL", False),
        )
    raise ValueError(
        f"未知的 STORAGE_BACKEND：{backend}（可选 local / s3）"
    )
