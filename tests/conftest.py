from __future__ import annotations

import pytest

from app import deps


@pytest.fixture(autouse=True)
def isolate_trace_files(tmp_path, monkeypatch):
    """所有测试的 trace 都写到临时目录，避免污染仓库 data/。"""
    monkeypatch.setenv("TRACE_ROOT", str(tmp_path / "traces"))
    get_trace_store = getattr(deps, "get_trace_store", None)
    if get_trace_store is not None:
        get_trace_store.cache_clear()
    yield
    if get_trace_store is not None:
        get_trace_store.cache_clear()


@pytest.fixture()
def metric_scope():
    from uuid import uuid4
    from sqlalchemy import text
    from src.db.base import create_db_engine

    engine = create_db_engine()
    suffix = uuid4().hex[:10]
    tenant, kb = f"t_metric_{suffix}", f"kb_metric_{suffix}"
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(
                text("INSERT INTO tenants (id, name) VALUES (:t, 'metrics')"),
                {"t": tenant},
            )
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases "
                    "(id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, 'metrics', '', 'active')"
                ),
                {"kb": kb, "t": tenant},
            )
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"数据库不可用：{exc}")
    yield engine, tenant, kb
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_http_metrics WHERE tenant_id = :t"), {"t": tenant}
        )
        connection.execute(
            text("DELETE FROM ops_rag_metrics WHERE tenant_id = :t"), {"t": tenant}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": kb}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
    engine.dispose()
