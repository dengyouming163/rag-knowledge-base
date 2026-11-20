"""驾驶舱任务看板与 trace 列表接口：等待/耗时指标、事件时间线、租户隔离。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.ingest_events import record_job_event
from src.ingest_jobs import enqueue

TENANT = "t_ops_jobs_api"
KB = "kb_ops_jobs_api"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过驾驶舱任务接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops jobs api')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'ops jobs kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingest_job_events WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def client(engine, monkeypatch) -> TestClient:
    # parser 未配置：避免测试发起真实网络探测
    monkeypatch.delenv("PARSER_BASE_URL", raising=False)
    return TestClient(create_app())


def test_ops_jobs_returns_wait_and_duration(client: TestClient, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_ops_job")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='succeeded', started_at=now(), "
                "finished_at=now() + interval '2 seconds', progress=100 WHERE id=:id"
            ),
            {"id": job_id},
        )

    body = client.get(
        "/v1/ops/jobs", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    ).json()
    job = next(item for item in body["jobs"] if item["id"] == job_id)
    assert job["wait_seconds"] is not None
    assert job["duration_seconds"] == 2.0


def test_ops_jobs_filters_by_status_and_counters(client: TestClient, engine) -> None:
    succeeded_id = enqueue(engine, TENANT, KB, doc_id="doc_ops_filter_done")
    enqueue(engine, TENANT, KB, doc_id="doc_ops_filter_queued")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='succeeded', started_at=now(), "
                "finished_at=now() WHERE id=:id"
            ),
            {"id": succeeded_id},
        )

    body = client.get(
        "/v1/ops/jobs",
        params={"kb_id": KB, "status": "succeeded"},
        headers={"X-Tenant-Id": TENANT},
    ).json()

    assert [item["id"] for item in body["jobs"]] == [succeeded_id]
    assert body["counts"]["succeeded"] == 1
    assert body["counts"]["queued"] == 1
    assert body["counts"]["running"] == 0


def test_ops_job_events_are_tenant_scoped(client: TestClient, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_ops_events")
    record_job_event(
        engine, job_id=job_id, tenant_id=TENANT,
        stage="parsing", status="started", progress=30,
    )
    body = client.get(
        f"/v1/ops/jobs/{job_id}/events", headers={"X-Tenant-Id": TENANT}
    ).json()
    assert body["events"][0]["stage"] == "parsing"
    assert client.get(
        f"/v1/ops/jobs/{job_id}/events", headers={"X-Tenant-Id": "other"}
    ).status_code == 404


def test_ops_traces_returns_recent_records(client: TestClient, tmp_path) -> None:
    from app.deps import get_trace_store
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    store.record(
        trace_id="trace-api", tenant=TENANT, kb=KB, stage="retrieve",
        ms=1.0, hits=2, refused=False, mode="hybrid", query="退款",
    )
    client.app.dependency_overrides[get_trace_store] = lambda: store
    try:
        body = client.get(
            "/v1/ops/traces",
            params={"kb_id": KB},
            headers={"X-Tenant-Id": TENANT},
        ).json()
    finally:
        client.app.dependency_overrides.pop(get_trace_store, None)
    assert body["traces"][0]["trace_id"] == "trace-api"
