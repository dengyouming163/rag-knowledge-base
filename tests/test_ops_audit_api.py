"""运维审计接口：仅管理员可读；开关关闭或非管理员一律 403 并写审计记录。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_ops_audit"
KB = "kb_ops_audit"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过审计接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops audit')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'audit kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_admin', :t, 'Admin', ARRAY['dept:cs'], 'admin')"
            ),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_member', :t, 'Member', ARRAY['dept:legal'], 'member')"
            ),
            {"t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def _audit_rows(engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT actor, action, result, reason FROM ops_audit "
                "WHERE tenant_id = :t ORDER BY id DESC"
            ),
            {"t": TENANT},
        ).mappings().all()
    return [dict(row) for row in rows]


def test_audit_requires_admin_and_switch(client: TestClient, engine, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "admin required"
    rows = _audit_rows(engine)
    assert rows[0]["action"] == "audit"
    assert rows[0]["actor"] == "u_member"
    assert rows[0]["result"] == "denied"
    assert rows[0]["reason"] == "admin required"


def test_audit_denied_when_actions_disabled(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "0")

    response = client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "ops actions disabled"
    rows = _audit_rows(engine)
    assert rows[0]["result"] == "denied"
    assert rows[0]["reason"] == "ops actions disabled"


def test_audit_lists_records_for_admin(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )

    response = client.get(
        "/v1/ops/audit?limit=10",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )

    assert response.status_code == 200
    audit = response.json()["audit"]
    assert len(audit) == 1
    assert audit[0]["actor"] == "u_member"
    assert audit[0]["result"] == "denied"
    assert audit[0]["detail"] == {}
    assert audit[0]["created_at"]
