"""文档上传接口测试：`POST /v1/kbs/{kb_id}/documents`。

覆盖四类风险：
1. 合法 PDF 上传返回 202（异步处理），并带回 job_id / doc_id；
2. 上传到其他租户的知识库返回 404，不能跨租户写入；
3. 后缀不在白名单内返回 422；
4. 空文件返回 422。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
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
def client() -> TestClient:
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过接口集成测试：{exc}")

    _cleanup(db_engine)
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

    yield TestClient(create_app())

    _cleanup(db_engine)
    db_engine.dispose()
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def test_upload_pdf_returns_202_and_job(client: TestClient, sample_pdf_bytes: bytes) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] > 0
    assert body["doc_id"].startswith(f"{KB}-")


def test_upload_to_other_tenant_kb_returns_404(
    client: TestClient, sample_pdf_bytes: bytes
) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Tenant-Id": OTHER_TENANT},
    )

    assert response.status_code == 404


def test_upload_rejects_non_pdf(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.docx", b"not a pdf", "application/octet-stream")},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 422


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 422
