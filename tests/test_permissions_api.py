"""权限解释 API：跨租户 404，同租户按真实召回口径解释 ACL/RLS 判定。"""

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
VECTOR = "[" + ",".join(["0.01"] * DIM) + "]"


def _seed(engine) -> None:
    """kb_identity：公开子块 + 受限子块 + 父块；kb_identity_locked：只有受限子块。"""
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
            # 父块 embedding 为空且不参与召回，计入统计会让口径虚高
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


def test_permissions_cross_tenant_returns_404(client: TestClient) -> None:
    response = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": "t_other", "X-User-Id": "u_admin"},
    )
    assert response.status_code == 404


def test_permissions_missing_kb_returns_404(client: TestClient) -> None:
    response = client.get(
        "/v1/ops/permissions",
        params={"kb_id": "kb_identity_missing"},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )
    assert response.status_code == 404


def test_permissions_member_counts_only_visible_retrievable_chunks(
    client: TestClient,
) -> None:
    body = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    ).json()

    assert body["acl"]["mode"] == "public_or_tag_intersection"
    assert body["acl"]["user_tags"] == ["dept:legal"]
    assert body["decision"] == "allow"
    # 公开子块可见；受限子块被 ACL 拦；父块不参与召回
    assert body["rls"]["scoped_count"] == 1
    # 没有 app.tenant_id 的连接被 RLS 过滤成 0 行
    assert body["rls"]["enabled"] is True
    assert body["rls"]["policy_count"] >= 1
    assert body["rls"]["bare_count"] == 0
    assert body["reason"] == "租户匹配，ACL 可见，RLS 已启用"


def test_permissions_admin_bypasses_acl_but_stays_in_scope(client: TestClient) -> None:
    body = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    ).json()

    assert body["acl"]["mode"] == "admin_bypass"
    assert body["decision"] == "allow"
    # 两个子块都可见；父块依旧不计入
    assert body["rls"]["scoped_count"] == 2


def test_permissions_denies_when_acl_filters_everything(client: TestClient) -> None:
    body = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB_LOCKED},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    ).json()

    assert body["decision"] == "deny"
    assert body["rls"]["scoped_count"] == 0
    assert body["reason"]
