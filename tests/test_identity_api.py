"""身份卡 API：角色与标签只能来自数据库，未知用户降级为 guest。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.db.session import tenant_scope

TENANT = "t_identity"
KB = "kb_identity"
KB_LOCKED = "kb_identity_locked"

DIM = 1024
# 固定小向量：维度必须与 chunks.embedding 的 vector(1024) 一致，否则插入被拒
VECTOR = "[" + ",".join(["0.01"] * DIM) + "]"


def _seed(engine) -> None:
    """两个库：正常库（公开子块 + 受限子块 + 父块）、全受限库。"""
    with tenant_scope(engine, TENANT, write=True) as connection:
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
        for kb_id in (KB, KB_LOCKED):
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, 'identity kb', '', 'active')"
                ),
                {"kb": kb_id, "t": TENANT},
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
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, "
                "content_hash, status, meta) "
                "VALUES ('doc_identity', :t, :kb, 'identity', '', 'ident_doc_hash', "
                "'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )
        chunks = [
            # (chunk_id, content_hash, text, meta, is_parent, embedding)
            ("ident#public", "ident_public_hash", "公开内容", "{}", False, VECTOR),
            (
                "ident#secret",
                "ident_secret_hash",
                "受限内容",
                '{"acl_tags": ["dept:secret"]}',
                False,
                VECTOR,
            ),
            # 父块只补上下文，不参与召回，因此不应计入 scoped_count
            ("ident#parent", "ident_parent_hash", "父块内容", "{}", True, None),
        ]
        for chunk_id, content_hash, body, meta, is_parent, vector in chunks:
            connection.execute(
                text(
                    "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, "
                    "text, token_count, embedding, embed_model_version, meta, is_parent) "
                    "VALUES (:chunk, :t, :kb, 'doc_identity', :hash, :body, 5, "
                    "CAST(:vec AS vector), 'test', CAST(:meta AS jsonb), :is_parent)"
                ),
                {
                    "chunk": chunk_id,
                    "t": TENANT,
                    "kb": KB,
                    "hash": content_hash,
                    "body": body,
                    "vec": vector,
                    "meta": meta,
                    "is_parent": is_parent,
                },
            )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, "
                "content_hash, status, meta) "
                "VALUES ('doc_identity_locked', :t, :kb, 'locked', '', "
                "'ident_locked_doc_hash', 'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB_LOCKED},
        )
        connection.execute(
            text(
                "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, "
                "text, token_count, embedding, embed_model_version, meta, is_parent) "
                "VALUES ('ident#locked', :t, :kb, 'doc_identity_locked', "
                "'ident_locked_hash', '仅密级可见', 5, CAST(:vec AS vector), 'test', "
                'CAST(\'{"acl_tags": ["dept:secret"]}\' AS jsonb), false)'
            ),
            {"t": TENANT, "kb": KB_LOCKED, "vec": VECTOR},
        )


def _cleanup(engine) -> None:
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


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
    # 隔离声明必须来自真实的 RLS 元数据，而不是写死
    assert body["isolation"]["rls"]["enabled"] is True
    assert body["isolation"]["rls"]["policy_count"] >= 1


def test_unknown_user_falls_back_to_guest(client: TestClient) -> None:
    body = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "ghost"},
    ).json()
    assert body["role"] == "guest"
    assert body["is_admin"] is False
    assert body["tags"] == []


def test_ops_actions_follow_env_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")

    body = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    ).json()

    assert body["ops"]["actions_enabled"] is True
    assert body["ops"]["can_trigger"] is True
