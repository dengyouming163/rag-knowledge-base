"""入库任务查询接口测试。

覆盖三类风险：
1. 单个任务必须带租户查询，跨租户一律 404；
2. 列表按 id 倒序，最多返回 20 条；
3. 列表只返回当前租户的数据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.ingest_jobs import enqueue

TENANT = "t_api_jobs"
OTHER_TENANT = "t_api_jobs_other"
KB = "kb_api_jobs"
OTHER_KB = "kb_api_jobs_other"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过接口集成测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'api jobs')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'jobs kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'other kb', '', 'active')"
            ),
            {"kb": OTHER_KB, "t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def test_get_job_returns_job_for_owner_tenant(client: TestClient, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_job_1")

    response = client.get(f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": TENANT})

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == job_id
    assert body["status"] == "queued"
    assert body["doc_id"] == "doc_job_1"


def test_get_job_returns_404_for_other_tenant(client: TestClient, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_job_2")

    response = client.get(f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": OTHER_TENANT})

    assert response.status_code == 404


def test_get_job_returns_404_when_missing(client: TestClient) -> None:
    response = client.get("/v1/jobs/999999999", headers={"X-Tenant-Id": TENANT})

    assert response.status_code == 404


def test_list_jobs_returns_latest_20_desc(client: TestClient, engine) -> None:
    job_ids = [enqueue(engine, TENANT, KB, doc_id=f"doc_list_{i}") for i in range(25)]

    response = client.get(
        "/v1/jobs", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert len(jobs) == 20
    assert [job["id"] for job in jobs] == sorted(job_ids, reverse=True)[:20]


def test_list_jobs_is_scoped_by_tenant_and_kb(client: TestClient, engine) -> None:
    enqueue(engine, TENANT, KB, doc_id="doc_scope_1")
    enqueue(engine, TENANT, OTHER_KB, doc_id="doc_scope_other")

    response = client.get(
        "/v1/jobs", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert [job["doc_id"] for job in jobs] == ["doc_scope_1"]

    other_response = client.get(
        "/v1/jobs", params={"kb_id": KB}, headers={"X-Tenant-Id": OTHER_TENANT}
    )
    assert other_response.status_code == 200
    assert other_response.json()["jobs"] == []
