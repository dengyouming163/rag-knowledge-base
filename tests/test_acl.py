"""Task 3.1：文档级 ACL 测试。

两条底线：
1. 无权的内容**不能出现在结果里**（不是"返回了再前端隐藏"）；
2. 没有标签的用户只能看到公开内容（fail-closed）。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text

from src.acl import acl_condition, load_user_tags, set_chunk_acl
from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine
from src.store_pg import PgVectorStore

DIM = 1024
TENANT = "t_acl"
KB = "kb_acl"
USER_LEGAL = "u_legal"

TEXTS = {
    "public": "退款政策：支持七天无理由退款。",
    "legal": "合同违约条款：需法务审核后执行。",
    "finance": "成本核算口径：含税价除以 1.13。",
}


def _vectors(count: int, seed: int = 11) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.uniform(-1.0, 1.0) for _ in range(DIM)] for _ in range(count)]


def _make_chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"{KB}#{name}",
            doc_id="doc_acl",
            text=value,
            title="ACL 文档",
            section_path=["权限"],
            source_url="",
            content_hash=f"acl_hash_{name}",
            audience="public",
            token_count=estimate_tokens(value),
        )
        for name, value in TEXTS.items()
    ]


# ---------------------------------------------------------------- 纯函数


def test_acl_condition_is_fail_closed() -> None:
    sql, params = acl_condition([])

    assert "?|" in sql and "coalesce" in sql
    assert params == {"user_tags": []}

    sql, params = acl_condition(["dept:legal"])
    assert params["user_tags"] == ["dept:legal"]


# ---------------------------------------------------------------- 连库


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过集成测试：{exc}")
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    db_engine.dispose()


@pytest.fixture()
def seeded(engine):
    chunks = _make_chunks()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'acl')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'acl kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_acl', :t, :kb, 'ACL 文档', '', 'acl_doc_hash', 'active', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )

    store = PgVectorStore(engine, tenant_id=TENANT, kb_id=KB, embed_model_version="test")
    store.upsert(chunks, _vectors(len(chunks)))

    set_chunk_acl(engine, [f"{KB}#legal"], ["dept:legal"])
    set_chunk_acl(engine, [f"{KB}#finance"], ["dept:finance"])

    yield store
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT})


def _ids(hits) -> set[str]:
    return {hit.chunk_id for hit in hits}


def test_anonymous_sees_only_public(seeded) -> None:
    hits = seeded.search([0.0] * DIM + [1.0] * 0, top_k=10, filters={"user_tags": []})

    assert _ids(hits) == {f"{KB}#public"}


def test_user_sees_own_tag_plus_public(seeded) -> None:
    hits = seeded.search([1.0] * DIM, top_k=10, filters={"user_tags": ["dept:legal"]})

    assert _ids(hits) == {f"{KB}#public", f"{KB}#legal"}


def test_user_cannot_see_other_department(seeded) -> None:
    hits = seeded.search([1.0] * DIM, top_k=10, filters={"user_tags": ["dept:finance"]})

    assert f"{KB}#legal" not in _ids(hits)
    assert f"{KB}#finance" in _ids(hits)


def test_missing_user_tags_means_public_only(seeded) -> None:
    hits = seeded.search([1.0] * DIM, top_k=10)

    assert _ids(hits) == {f"{KB}#public"}


def test_load_user_tags(engine, seeded) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags) "
                "VALUES (:u, :t, '法务', ARRAY['dept:legal','level:internal'])"
            ),
            {"u": USER_LEGAL, "t": TENANT},
        )

    assert set(load_user_tags(engine, TENANT, USER_LEGAL)) == {"dept:legal", "level:internal"}
    # 不存在的用户不报错，等价于"只能看公开内容"
    assert load_user_tags(engine, TENANT, "u_ghost") == []


def test_restrict_public_chunk_hides_it(engine, seeded) -> None:
    set_chunk_acl(engine, [f"{KB}#public"], ["dept:legal"])

    anonymous = seeded.search([1.0] * DIM, top_k=10)
    legal = seeded.search([1.0] * DIM, top_k=10, filters={"user_tags": ["dept:legal"]})

    assert f"{KB}#public" not in _ids(anonymous)
    assert f"{KB}#public" in _ids(legal)


def test_set_chunk_acl_empty_ids_is_noop(engine) -> None:
    assert set_chunk_acl(engine, [], ["dept:legal"]) == 0
