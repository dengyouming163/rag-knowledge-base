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


def test_observability_tables_exist(engine) -> None:
    assert {"ops_http_metrics", "ops_rag_metrics"} <= set(
        inspect(engine).get_table_names()
    )


def test_rag_metric_constraints_and_indexes(engine) -> None:
    inspector = inspect(engine)
    columns = {item["name"] for item in inspector.get_columns("ops_rag_metrics")}
    assert {
        "trace_id", "operation", "outcome", "total_ms", "stage_spans",
        "rewrite_ms", "retrieval_ms", "rerank_ms", "llm_ms",
    } <= columns
    uniques = inspector.get_unique_constraints("ops_rag_metrics")
    assert any(
        item["name"] == "uq_ops_rag_metrics_tenant_trace"
        and item["column_names"] == ["tenant_id", "trace_id"]
        for item in uniques
    )


def test_metric_tables_have_no_foreign_keys(engine) -> None:
    inspector = inspect(engine)
    assert inspector.get_foreign_keys("ops_http_metrics") == []
    assert inspector.get_foreign_keys("ops_rag_metrics") == []
