"""文档上传接口测试：`POST /v1/kbs/{kb_id}/documents`。

覆盖的风险：
1. 合法 PDF 上传返回 202（异步处理），并带回 job_id / doc_id；
2. 上传到其他租户的知识库返回 404，不能跨租户写入；
3. 后缀不在白名单内返回 422，空文件 422，超过大小上限 413；
4. 同一份文件重复上传幂等：复用 doc_id，只落一份文件；
5. 成功后 documents 行是 pending，且文件真实存在。
"""

from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from app.routers import documents
from src.db.base import create_db_engine

TENANT = "t_docs"
OTHER_TENANT = "t_docs_other"
KB = "kb_docs"
UPLOAD_ROOT = Path("data/uploads")


@pytest.fixture()
def sample_pdf_bytes() -> bytes:
    # 本任务不解析 PDF，只要是一个非空、后缀正确的文件即可
    return b"%PDF-1.4\n%%EOF"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过接口集成测试：{exc}")

    _cleanup(db_engine)
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'docs')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'docs kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def _upload(client: TestClient, content: bytes, filename: str = "policy.pdf"):
    return client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": (filename, content, "application/pdf")},
        headers={"X-Tenant-Id": TENANT},
    )


def test_upload_pdf_returns_202_and_job(client: TestClient, sample_pdf_bytes: bytes) -> None:
    response = _upload(client, sample_pdf_bytes)

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] > 0
    assert body["doc_id"].startswith(f"{KB}-")
    assert body["duplicated"] is False


def test_upload_to_other_tenant_kb_returns_404(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Tenant-Id": OTHER_TENANT},
    )

    assert response.status_code == 404


def test_upload_rejects_unsupported_suffix(client: TestClient) -> None:
    response = _upload(client, b"legacy office", filename="policy.doc")

    assert response.status_code == 422


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = _upload(client, b"")

    assert response.status_code == 422


def test_duplicate_upload_reuses_document_and_file(
    client: TestClient, engine, sample_pdf_bytes: bytes
) -> None:
    first = _upload(client, sample_pdf_bytes)
    second = _upload(client, sample_pdf_bytes)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["doc_id"] == first.json()["doc_id"]
    assert second.json()["duplicated"] is True

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM documents "
                "WHERE tenant_id = :t AND kb_id = :kb"
            ),
            {"t": TENANT, "kb": KB},
        ).scalar_one()
    assert count == 1

    files = [path for path in (UPLOAD_ROOT / TENANT / KB).rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].name == f"{first.json()['doc_id']}.pdf"


def test_upload_too_large_returns_413(client: TestClient, monkeypatch) -> None:
    assert documents.DEFAULT_MAX_UPLOAD_BYTES == 50 * 1024 * 1024
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    assert _upload(client, b"x" * 11).status_code == 413


def test_upload_too_large_persists_nothing(client: TestClient, engine, monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    assert _upload(client, b"x" * 11).status_code == 413
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    assert count == 0
    assert not (UPLOAD_ROOT / TENANT / KB).exists()


def test_upload_reads_only_one_byte_past_limit(monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    monkeypatch.setattr(documents, "ensure_kb", lambda *_args: None)
    upload = UploadFile(file=io.BytesIO(b"x" * 11), filename="policy.pdf")
    original_read, sizes = upload.read, []

    async def recording_read(size: int = -1) -> bytes:
        sizes.append(size)
        return await original_read(size)

    monkeypatch.setattr(upload, "read", recording_read)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(documents.upload_document(KB, TENANT, None, upload))
    assert exc_info.value.status_code == 413
    assert sizes == [11]


def test_max_upload_bytes_falls_back_on_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "not-a-number")
    assert documents.max_upload_bytes() == documents.DEFAULT_MAX_UPLOAD_BYTES


def test_upload_persists_pending_document_and_file(
    client: TestClient, engine, sample_pdf_bytes: bytes
) -> None:
    response = _upload(client, sample_pdf_bytes)
    doc_id = response.json()["doc_id"]

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT status, content_hash FROM documents "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": doc_id, "t": TENANT},
        ).mappings().one()
    assert row["status"] == "pending"
    assert len(row["content_hash"]) == 16

    # 落盘文件名只由服务端的 doc_id + 后缀组成
    assert (UPLOAD_ROOT / TENANT / KB / f"{doc_id}.pdf").is_file()


@pytest.mark.parametrize("filename", ["policy.docx", "policy.pptx", "policy.xlsx"])
def test_upload_accepts_office_formats(client: TestClient, filename: str) -> None:
    response = _upload(client, f"office-{filename}".encode(), filename=filename)

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]
    suffix = Path(filename).suffix
    assert (UPLOAD_ROOT / TENANT / KB / f"{doc_id}{suffix}").is_file()


def test_upload_stores_filename_and_size_in_meta(
    client: TestClient, engine, sample_pdf_bytes: bytes
) -> None:
    response = _upload(client, sample_pdf_bytes, filename="report.pdf")

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]

    with engine.connect() as connection:
        meta = connection.execute(
            text("SELECT meta FROM documents WHERE id = :id"), {"id": doc_id}
        ).scalar_one()

    assert meta["filename"] == "report.pdf"
    assert meta["size"] == len(sample_pdf_bytes)
