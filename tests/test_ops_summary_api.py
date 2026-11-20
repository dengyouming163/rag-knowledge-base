"""驾驶舱摘要接口测试：结构完整、租户隔离、parser 未配置不探测网络。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.ingest_jobs import enqueue

TENANT = "t_ops_summary"
OTHER_TENANT = "t_ops_summary_other"
KB = "kb_ops_summary"
OTHER_KB = "kb_ops_summary_other"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过摘要接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops summary')"),
            {"t": TENANT},
        )
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops summary other')"),
            {"t": OTHER_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'summary kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'other kb', '', 'active')"
            ),
            {"kb": OTHER_KB, "t": OTHER_TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )


@pytest.fixture()
def client(engine, monkeypatch) -> TestClient:
    # parser 未配置：避免测试发起真实网络探测
    monkeypatch.delenv("PARSER_BASE_URL", raising=False)
    return TestClient(create_app())


def test_summary_returns_cockpit_structure(client: TestClient, engine) -> None:
    enqueue(engine, TENANT, KB, doc_id="doc_ops_summary_1")

    response = client.get(f"/v1/ops/summary?kb_id={KB}", headers={"X-Tenant-Id": TENANT})

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"health", "queue", "ai", "latest_runs", "latest_workers"}
    assert body["health"]["parser"] == "unconfigured"
    assert body["health"]["api"] == "up"
    assert isinstance(body["queue"]["total"], int)
    assert body["queue"]["total"] >= 1
    assert isinstance(body["ai"]["requests"], int)
    assert body["ai"]["requests"] == 0
    assert isinstance(body["latest_runs"], list)
    assert isinstance(body["latest_workers"], list)


def test_summary_returns_404_for_other_tenant_kb(client: TestClient) -> None:
    response = client.get(
        f"/v1/ops/summary?kb_id={OTHER_KB}", headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 404


def test_summary_returns_404_when_kb_id_belongs_to_other_tenant(client: TestClient) -> None:
    response = client.get(
        f"/v1/ops/summary?kb_id={KB}", headers={"X-Tenant-Id": OTHER_TENANT}
    )

    assert response.status_code == 404
