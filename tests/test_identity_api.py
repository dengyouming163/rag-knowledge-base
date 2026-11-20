"""身份卡 API：角色与标签只能来自数据库，未知用户降级为 guest。"""

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


def test_identity_returns_role_and_tags(client: TestClient) -> None:
    response = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == "u_admin"
    assert body["role"] == "admin"
    assert body["is_admin"] is True
    assert body["tags"] == ["dept:cs"]
    assert KB in body["kbs"]
    assert body["isolation"]["rls"]["enabled"] in (True, False)


def test_unknown_user_falls_back_to_guest(client: TestClient) -> None:
    body = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "ghost"},
    ).json()
    assert body["role"] == "guest"
    assert body["is_admin"] is False
    assert body["tags"] == []
