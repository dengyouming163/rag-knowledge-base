"""存储抽象单测：本地读写删、S3 请求参数与错误传播、后端选择。"""

from __future__ import annotations

import io

import boto3
import pytest
from botocore.stub import Stubber

from src.storage import LocalStorage, S3Storage, StorageError, build_storage


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9000",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def test_local_roundtrip_and_delete(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    storage.put_bytes("t/kb/doc1.docx", b"payload")

    assert storage.exists("t/kb/doc1.docx") is True
    assert storage.get_bytes("t/kb/doc1.docx") == b"payload"
    assert (tmp_path / "t" / "kb" / "doc1.docx").read_bytes() == b"payload"

    storage.delete("t/kb/doc1.docx")

    assert storage.exists("t/kb/doc1.docx") is False
    with pytest.raises(FileNotFoundError):
        storage.get_bytes("t/kb/doc1.docx")


def test_local_rejects_path_traversal(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(StorageError, match="非法存储 key"):
        storage.put_bytes("../escape.txt", b"x")


def test_local_write_error_propagates(tmp_path) -> None:
    root = tmp_path / "root"
    root.write_text("not a directory", encoding="utf-8")
    storage = LocalStorage(root)

    with pytest.raises(OSError):
        storage.put_bytes("t/kb/doc1.md", b"x")


def test_s3_roundtrip_uses_bucket_and_key() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx", "Body": b"payload"},
        )
        stubber.add_response(
            "head_object", {}, {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"}
        )
        stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(b"payload")},
            {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"},
        )
        stubber.add_response(
            "delete_object", {}, {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"}
        )

        storage.put_bytes("t/kb/doc1.docx", b"payload")
        assert storage.exists("t/kb/doc1.docx") is True
        assert storage.get_bytes("t/kb/doc1.docx") == b"payload"
        storage.delete("t/kb/doc1.docx")


def test_s3_exists_returns_false_for_missing_key() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object", service_error_code="404", http_status_code=404
        )

        assert storage.exists("t/kb/missing.md") is False


def test_s3_get_missing_key_raises_file_not_found() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_object", service_error_code="NoSuchKey", http_status_code=404
        )

        with pytest.raises(FileNotFoundError):
            storage.get_bytes("t/kb/missing.md")


def test_s3_put_failure_becomes_storage_error() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="InternalError", http_status_code=500
        )

        with pytest.raises(StorageError, match="S3 写入失败"):
            storage.put_bytes("t/kb/doc1.md", b"x")


def test_build_storage_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    storage = build_storage()

    assert isinstance(storage, LocalStorage)


def test_build_storage_selects_s3(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_BUCKET", "rag-kb")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_USE_SSL", "false")

    storage = build_storage()

    assert isinstance(storage, S3Storage)
    assert storage.bucket == "rag-kb"


def test_build_storage_rejects_unknown_backend(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "ftp")

    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        build_storage()


def test_describe_storage_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    from src import storage

    assert storage.describe_storage() == {"backend": "local", "bucket": None}


def test_describe_storage_s3_with_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "rag-kb")

    from src import storage

    assert storage.describe_storage() == {"backend": "s3", "bucket": "rag-kb"}


def test_describe_storage_unknown_backend_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "weird")

    from src import storage

    assert storage.describe_storage()["backend"] == "local"
