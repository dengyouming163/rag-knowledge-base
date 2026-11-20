"""权限解释 API：跨租户 404，同租户解释 ACL/RLS 判定。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_identity"
KB = "kb_identity"


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
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'identity')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'identity kb', '', 'active')"
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
    with db_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    db_engine.dispose()


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def test_permissions_cross_tenant_returns_404(client: TestClient) -> None:
    response = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": "t_other", "X-User-Id": "u_admin"},
    )
    assert response.status_code == 404


def test_permissions_reports_acl_mode_and_rls_counts(client: TestClient) -> None:
    body = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    ).json()
    assert body["acl"]["mode"] == "public_or_tag_intersection"
    assert body["rls"]["bare_count"] == 0 or body["rls"]["enabled"] is False
    assert body["decision"] in {"allow", "deny"}
    assert body["reason"]
