"""RLS 验证：没有声明作用域的连接，查不到任何 chunks 行。

这是"忘了加 WHERE 也不会泄露"的证据 —— 不是靠代码自觉，而是数据库拒绝返回。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.db.session import tenant_scope

DIM = 1024
TENANT = "t_rls"
OTHER = "t_rls_other"


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过 RLS 集成测试：{exc}")
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = ANY(CAST(:ids AS text[]))"), {"ids": [TENANT, OTHER]})
    db_engine.dispose()


@pytest.fixture()
def seeded(engine):
    vector = "[" + ",".join(repr(random.Random(7).uniform(-1, 1)) for _ in range(DIM)) + "]"
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'rls')"), {"t": TENANT})
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES ('kb_rls', :t, 'rls kb', '', 'active')"
            ),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_rls', :t, 'kb_rls', 'rls', '', 'h_rls', 'ready', '{}'::jsonb)"
            ),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                "embedding, embed_model_version, meta) "
                "VALUES ('rls#1', :t, 'kb_rls', 'doc_rls', 'h_rls_c', '仅本租户可见', 5, "
                "CAST(:vec AS vector), 'test', '{}'::jsonb)"
            ),
            {"t": TENANT, "vec": vector},
        )
    return vector


def test_without_scope_returns_zero_rows(engine, seeded) -> None:
    with engine.connect() as connection:
        # 故意不设置 app.tenant_id：策略判定为假
        count = connection.execute(text("SELECT count(*) FROM chunks")).scalar_one()

    assert count == 0


def test_with_scope_sees_own_rows(engine, seeded) -> None:
    with tenant_scope(engine, TENANT) as connection:
        count = connection.execute(
            text("SELECT count(*) FROM chunks WHERE chunk_id = 'rls#1'")
        ).scalar_one()

    assert count == 1


def test_with_other_tenant_scope_sees_nothing(engine, seeded) -> None:
    with tenant_scope(engine, OTHER) as connection:
        count = connection.execute(
            text("SELECT count(*) FROM chunks WHERE chunk_id = 'rls#1'")
        ).scalar_one()

    assert count == 0


def test_insert_other_tenant_row_is_rejected(engine, seeded) -> None:
    """WITH CHECK 保证写不进别人的租户，即使显式写了 tenant_id。"""
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with tenant_scope(engine, TENANT, write=True) as connection:
            connection.execute(
                text(
                    "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                    "embed_model_version, meta) "
                    "VALUES ('rls#bad', :other, 'kb_rls', 'doc_rls', 'h_bad', 'x', 1, 'test', '{}'::jsonb)"
                ),
                {"other": OTHER},
            )
