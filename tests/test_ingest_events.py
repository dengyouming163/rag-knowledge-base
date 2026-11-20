"""Task 2：任务阶段事件与 worker 心跳（best-effort、按租户隔离）。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ingest_events import list_job_events, record_job_event
from src.ingest_jobs import enqueue, get_job, mark_failed, retry_job

TENANT = "t_events"
KB = "kb_events"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES ('t_events', 'events') "
                 "ON CONFLICT (id) DO NOTHING")
        )
        connection.execute(
            text("INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                 "VALUES ('kb_events', 't_events', 'events', '', 'active') "
                 "ON CONFLICT (id) DO NOTHING")
        )
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest_job_events WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM tenants WHERE id = 't_events'"))
    db_engine.dispose()


def test_events_are_ordered_and_scoped(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_events")

    record_job_event(
        engine, job_id=job_id, tenant_id=TENANT,
        stage="parsing", status="started", progress=30,
    )
    record_job_event(
        engine, job_id=job_id, tenant_id=TENANT,
        stage="succeeded", status="succeeded", progress=100,
    )

    events = list_job_events(engine, TENANT, job_id)
    assert [event["stage"] for event in events] == ["parsing", "succeeded"]
    assert list_job_events(engine, "t_other", job_id) == []


def test_event_write_failure_does_not_raise(engine, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(engine, "begin", boom)
    record_job_event(
        engine, job_id=1, tenant_id=TENANT,
        stage="failed", status="failed", progress=0, message="boom",
    )
    # engine fixture 的清理依赖 engine.begin，必须在 fixture 结束前恢复
    monkeypatch.undo()


def test_retry_job_only_allows_failed_jobs(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry")

    assert retry_job(engine, TENANT, job_id) is None
    assert get_job(engine, TENANT, job_id)["status"] == "queued"

    mark_failed(engine, job_id, "boom", retry=False)
    assert get_job(engine, TENANT, job_id)["status"] == "failed"

    retried = retry_job(engine, TENANT, job_id)
    assert retried is not None
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0
    assert retried["progress"] == 0
    assert retried["error"] == ""
    assert retried["finished_at"] is None


def test_retry_job_rejects_other_tenant(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry_other")
    mark_failed(engine, job_id, "boom", retry=False)

    assert retry_job(engine, "t_other", job_id) is None
    assert get_job(engine, TENANT, job_id)["status"] == "failed"
