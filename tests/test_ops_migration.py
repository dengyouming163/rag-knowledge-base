"""调测驾驶舱表结构测试：只校验迁移后的真实 schema，不涉及 CRUD/API。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from src.db.base import create_db_engine


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    yield db_engine
    db_engine.dispose()


def test_ops_tables_exist(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {"ops_runs", "ops_audit", "ingest_job_events", "ops_workers"} <= tables


def test_ingest_jobs_has_started_at(engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("ingest_jobs")}
    assert "started_at" in columns


def test_ops_runs_has_active_unique_index(engine) -> None:
    indexes = inspect(engine).get_indexes("ops_runs")
    active = [item for item in indexes if item["name"] == "uq_ops_runs_active_kind"]
    assert active and active[0]["unique"] is True


def test_ops_workers_has_no_tenant_id(engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("ops_workers")}
    assert "worker_id" in columns
    assert "tenant_id" not in columns
