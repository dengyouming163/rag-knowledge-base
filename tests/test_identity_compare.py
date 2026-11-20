"""身份切换对比 API：仅管理员可用，逐用户按真实召回口径返回命中数。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.db.session import tenant_scope

TENANT = "t_identity_compare"
KB = "kb_identity_compare"

DIM = 1024
VECTOR = "[" + ",".join(["0.01"] * DIM) + "]"


def _seed(engine) -> None:
    """一个库：公开子块 + 受限子块 + 父块，足够体现不同身份的可见差异。"""
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(
            text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'identity compare')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'compare kb', '', 'active')"
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
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, "
                "content_hash, status, meta) "
                "VALUES ('doc_compare', :t, :kb, 'compare', '', 'compare_doc_hash', "
                "'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )
        chunks = [
            ("cmp#public", "cmp_public_hash", "退款需要凭证", "{}", False, VECTOR),
            (
                "cmp#secret",
                "cmp_secret_hash",
                "退款内部口径",
                '{"acl_tags": ["dept:secret"]}',
                False,
                VECTOR,
            ),
            ("cmp#parent", "cmp_parent_hash", "退款父块", "{}", True, None),
        ]
        for chunk_id, content_hash, body, meta, is_parent, vector in chunks:
            connection.execute(
                text(
                    "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, "
                    "text, token_count, embedding, embed_model_version, meta, is_parent) "
                    "VALUES (:chunk, :t, :kb, 'doc_compare', :hash, :body, 5, "
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


def _cleanup(engine) -> None:
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(
            text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT}
        )
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


def test_compare_requires_admin(client: TestClient) -> None:
    response = client.post(
        f"/v1/ops/kbs/{KB}/identity-compare",
        json={"query": "退款", "user_ids": ["guest", "u_member"]},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )
    assert response.status_code == 403


def test_compare_returns_per_user_hits(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    response = client.post(
        f"/v1/ops/kbs/{KB}/identity-compare",
        json={"query": "退款", "user_ids": ["guest", "u_member", "u_admin"], "top_k": 5},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 200
    rows = response.json()["users"]
    assert [row["user"] for row in rows] == ["guest", "u_member", "u_admin"]
    assert all("hits" in row and "latency_ms" in row for row in rows)

    by_user = {row["user"]: row for row in rows}
    guest = by_user["guest"]
    member = by_user["u_member"]
    admin = by_user["u_admin"]

    # 公开块对所有人可见，且确实打出了分数（不是空召回）
    assert guest["hits"] >= 1
    assert guest["top_score"] is not None
    assert "cmp#public" in guest["visible_chunk_ids"]
    # dept:secret 受限块被 ACL 拦住：guest 与 member 都看不到
    assert "cmp#secret" not in guest["visible_chunk_ids"]
    assert "cmp#secret" not in member["visible_chunk_ids"]
    # 管理员旁路 ACL，同一 query 能看到更多
    assert "cmp#secret" in admin["visible_chunk_ids"]
    assert admin["hits"] > guest["hits"]


def test_compare_cross_tenant_returns_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    response = client.post(
        f"/v1/ops/kbs/{KB}/identity-compare",
        json={"query": "退款", "user_ids": ["u_admin"]},
        headers={"X-Tenant-Id": "t_identity_compare_other", "X-User-Id": "u_admin"},
    )
    assert response.status_code == 404
