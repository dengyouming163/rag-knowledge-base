"""Task 2：任务阶段事件与 worker 心跳（best-effort、按租户隔离）。"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ingest_events import (
    list_job_events,
    list_workers,
    record_job_event,
    record_worker_heartbeat,
)
from src.ingest_jobs import claim_next, enqueue, get_job, mark_failed, retry_job

TENANT = "t_events"
KB = "kb_events"


def _seed_document(engine, doc_id: str, status: str = "failed") -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:doc, :t, :kb, :doc, :url, :hash, :status, '{}'::jsonb)"
            ),
            {
                "doc": doc_id,
                "t": TENANT,
                "kb": KB,
                "url": f"{TENANT}/{KB}/{doc_id}.md",
                "hash": f"hash_{doc_id}",
                "status": status,
            },
        )


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

    # 用 context() 限定补丁生命周期，fixture 清理时 engine.begin 已恢复
    with monkeypatch.context() as patcher:
        patcher.setattr(engine, "begin", boom)
        record_job_event(
            engine, job_id=1, tenant_id=TENANT,
            stage="failed", status="failed", progress=0, message="boom",
        )


def test_retry_job_only_allows_failed_jobs(engine) -> None:
    _seed_document(engine, "doc_retry", status="pending")
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry")

    assert retry_job(engine, TENANT, job_id) is None
    assert get_job(engine, TENANT, job_id)["status"] == "queued"

    claimed = claim_next(engine)
    assert claimed is not None and claimed["started_at"] is not None
    mark_failed(engine, job_id, "boom", retry=False)
    assert get_job(engine, TENANT, job_id)["status"] == "failed"
    assert get_job(engine, TENANT, job_id)["started_at"] is not None
    # 模拟 worker 终态失败后同步文档状态
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE documents SET status = 'failed', updated_at = now() "
                "WHERE id = 'doc_retry' AND tenant_id = :t"
            ),
            {"t": TENANT},
        )

    retried = retry_job(engine, TENANT, job_id)
    assert retried is not None
    assert retried["status"] == "queued"
    assert retried["attempts"] == 0
    assert retried["progress"] == 0
    assert retried["error"] == ""
    assert retried["started_at"] is None
    assert retried["finished_at"] is None
    with engine.begin() as connection:
        doc_status = connection.execute(
            text("SELECT status FROM documents WHERE id = 'doc_retry' AND tenant_id = :t"),
            {"t": TENANT},
        ).scalar_one()
    assert doc_status == "pending"


def test_retry_job_rejects_other_tenant(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry_other")
    mark_failed(engine, job_id, "boom", retry=False)

    assert retry_job(engine, "t_other", job_id) is None
    assert get_job(engine, TENANT, job_id)["status"] == "failed"


def test_worker_heartbeat_upserts_single_row(engine) -> None:
    worker_id = "worker-test-events"
    try:
        record_worker_heartbeat(engine, worker_id=worker_id)
        record_worker_heartbeat(engine, worker_id=worker_id, current_job_id=42)

        rows = [row for row in list_workers(engine) if row["worker_id"] == worker_id]
        assert len(rows) == 1
        assert rows[0]["current_job_id"] == 42
        assert rows[0]["age_seconds"] >= 0
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ops_workers WHERE worker_id = :w"), {"w": worker_id}
            )
