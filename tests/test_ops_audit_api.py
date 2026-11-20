"""运维审计接口：仅管理员可读；开关关闭或非管理员一律 403 并写审计记录。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src import ops_audit
from src.db.base import create_db_engine
from src.ops_runs import enqueue

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


def _audit_count(engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM ops_audit WHERE tenant_id = :t"),
                {"t": TENANT},
            ).scalar_one()
        )


def test_audit_requires_admin_and_switch(client: TestClient, engine, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    response = client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "admin required"
    # 读接口被拒不得写审计（否则任意调用方都能污染审计流水）
    assert _audit_count(engine) == 0


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
    assert _audit_count(engine) == 0


def test_audit_lists_records_for_admin(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    ops_audit.record(
        engine, tenant_id=TENANT, actor="u_system", action="eval",
        target="run:1", result="allowed",
    )

    response = client.get(
        "/v1/ops/audit?limit=10",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )

    assert response.status_code == 200
    audit = response.json()["audit"]
    assert len(audit) == 1
    assert audit[0]["actor"] == "u_system"
    assert audit[0]["result"] == "allowed"
    assert audit[0]["detail"] == {}
    assert audit[0]["created_at"]


def test_run_detail_404_for_missing_or_other_tenant(
    client: TestClient, engine
) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")

    found = client.get(f"/v1/ops/runs/{run_id}", headers={"X-Tenant-Id": TENANT})
    missing = client.get("/v1/ops/runs/999999", headers={"X-Tenant-Id": TENANT})
    other_tenant = client.get(
        f"/v1/ops/runs/{run_id}", headers={"X-Tenant-Id": "t_ops_audit_other"}
    )

    assert found.status_code == 200
    assert found.json()["id"] == run_id
    assert missing.status_code == 404
    assert other_tenant.status_code == 404
