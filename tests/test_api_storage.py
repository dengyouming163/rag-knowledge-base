"""存储视图接口测试：GET /v1/storage。

覆盖：响应结构、kb/status 过滤、租户隔离、非法 kb_id→400、local 后端 bucket 为 null。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_storage"
OTHER_TENANT = "t_storage_other"
KB = "kb_storage"
KB2 = "kb_storage_2"
UPLOAD_ROOT = Path("data/uploads")


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
            text("INSERT INTO tenants (id, name) VALUES (:t, 'storage')"), {"t": TENANT}
        )
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'other')"), {"t": OTHER_TENANT}
        )
        for kb in (KB, KB2):
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, :kb, '', 'active')"
                ),
                {"kb": kb, "t": TENANT},
            )
        # documents.kb_id 有外键，OTHER_TENANT 的文档也要先建 kb
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES ('kb_x', :t, 'kb_x', '', 'active')"
            ),
            {"t": OTHER_TENANT},
        )

    # tenants/kbs 必须先提交，再在独立连接里插 documents——否则内层连接
    # 看不到外层未提交的行，documents.tenant_id 外键会报 violation。
    _insert_doc(db_engine, TENANT, KB, "doc-ready", "ready", "政策.docx", 82000)
    _insert_doc(db_engine, TENANT, KB, "doc-pending", "pending", "数据.xlsx", 1200000)
    _insert_doc(db_engine, TENANT, KB2, "doc-failed", "failed", "扫描.pdf", 3400000)
    _insert_doc(db_engine, OTHER_TENANT, "kb_x", "doc-other", "ready", "别户.md", 100)

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        for t in (TENANT, OTHER_TENANT):
            connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})


def _insert_doc(engine, tenant, kb, doc_id, status, filename, size) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:id, :t, :kb, :title, :url, :hash, :status, CAST(:meta AS jsonb))"
            ),
            {
                "id": doc_id,
                "t": tenant,
                "kb": kb,
                "title": doc_id,
                "url": f"{tenant}/{kb}/{doc_id}",
                "hash": doc_id,
                "status": status,
                "meta": json.dumps({"filename": filename, "size": size}, ensure_ascii=False),
            },
        )


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def _list(client, **params):
    return client.get("/v1/storage", params=params, headers={"X-Tenant-Id": TENANT})


def test_storage_response_shape_and_content(client: TestClient) -> None:
    resp = _list(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] in {"local", "s3"}
    assert body["bucket"] is None or isinstance(body["bucket"], str)
    assert body["stats"]["total"] == 3
    assert body["stats"]["bytes"] == 82000 + 1200000 + 3400000
    assert body["stats"]["by_status"] == {"ready": 1, "pending": 1, "failed": 1}
    filenames = {d["filename"] for d in body["documents"]}
    assert {"政策.docx", "数据.xlsx", "扫描.pdf"} <= filenames
    for d in body["documents"]:
        assert set(d) == {"id", "kb_id", "filename", "size", "status", "created_at"}


def test_storage_filter_by_kb(client: TestClient) -> None:
    resp = _list(client, kb_id=KB)
    body = resp.json()
    assert body["stats"]["total"] == 2
    assert {d["kb_id"] for d in body["documents"]} == {KB}


def test_storage_filter_by_status(client: TestClient) -> None:
    resp = _list(client, status="failed")
    body = resp.json()
    assert body["stats"]["total"] == 1
    assert body["documents"][0]["status"] == "failed"


def test_storage_tenant_isolation(client: TestClient) -> None:
    resp = client.get("/v1/storage", headers={"X-Tenant-Id": OTHER_TENANT})
    body = resp.json()
    assert body["stats"]["total"] == 1
    assert body["documents"][0]["filename"] == "别户.md"


def test_storage_invalid_kb_id_returns_400(client: TestClient) -> None:
    resp = _list(client, kb_id="../evil")
    assert resp.status_code == 400


def test_storage_limit_caps_results(client: TestClient) -> None:
    resp = _list(client, limit=1)
    body = resp.json()
    assert len(body["documents"]) == 1
