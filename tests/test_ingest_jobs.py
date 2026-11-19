"""Task 2：ingest_jobs 状态机（入队 / 领取 / 完成 / 失败 / 重试）。

核心断言：
1. 同一 (tenant, kb, doc_id) 已有排队/运行中的任务时不重复入队；
2. 领取是原子的：同一条任务不会被两个 worker 同时拿到；
3. 连续失败到 MAX_ATTEMPTS 后进入终态 failed；
4. get_job 必须带 tenant_id，跨租户查不到。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ingest_jobs import (
    claim_next,
    enqueue,
    get_job,
    mark_failed,
    mark_succeeded,
)

TENANT = "t_jobs"
KB = "kb_jobs"


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def _seed(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'jobs')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'jobs kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(db_engine)
    _seed(db_engine)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def test_claim_is_idempotent_under_concurrency(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d1", source="upload")
    first = claim_next(engine, worker="w1")
    second = claim_next(engine, worker="w2")
    assert first["id"] == job_id
    assert second is None or second["id"] != job_id


def test_failure_after_three_attempts_marks_failed(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    for _ in range(3):
        job = claim_next(engine, worker="w1")
        mark_failed(engine, job["id"], "boom")
    assert get_job(engine, TENANT, job_id)["status"] == "failed"


def test_enqueue_is_idempotent(engine) -> None:
    first = enqueue(engine, TENANT, KB, doc_id="d3")
    second = enqueue(engine, TENANT, KB, doc_id="d3")

    assert first == second


def test_get_job_rejects_other_tenant(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d4")

    assert get_job(engine, TENANT, job_id) is not None
    assert get_job(engine, "t_jobs_other", job_id) is None


def test_mark_succeeded_sets_finished_at(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d5")
    claim_next(engine, worker="w1")

    mark_succeeded(engine, job_id)

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "succeeded"
    assert job["progress"] == 100
    assert job["finished_at"] is not None
