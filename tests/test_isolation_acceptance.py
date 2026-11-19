"""Task 3.3：隔离验收四件套（可演示版）

四组场景，每条都是"如果隔离失效，就会失败"的硬断言：
1. 跨租户：B 租户永远看不到 A 租户的块（应用过滤 + RLS 双保险）
2. 跨知识库：同租户内，kb_a2 的查询看不到 kb_a1 的块
3. ACL 越权：无标签用户看不到受限块，有标签用户能看到
4. 管理员边界：管理员能跨标签，但跨租户仍为空

运行：py -3.13 -m pytest tests/test_isolation_acceptance.py -v
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text

from src.acl import load_user_context, set_chunk_acl
from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine
from src.db.session import tenant_scope
from src.store_pg import PgVectorStore

DIM = 1024

TENANT_A = "t_iso_a"
TENANT_B = "t_iso_b"
KB_A1 = "kb_iso_a1"
KB_A2 = "kb_iso_a2"
KB_B1 = "kb_iso_b1"

TEXTS = {
    "pub": ("A 库公开政策：七天无理由退款。", []),
    "legal": ("A 库合同法务条款：违约需法务审核。", ["dept:legal"]),
    "finance": ("A 库成本口径：含税价除以 1.13。", ["dept:finance"]),
    "a2": ("A 库第二个知识库的内部资料。", []),
    "b": ("B 租户的私有资料。", []),
}


def _vec(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


def _chunk(chunk_id: str, doc_id: str, text_value: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text_value,
        title="隔离验收",
        section_path=["验收"],
        source_url="",
        content_hash=f"iso_{chunk_id}",
        token_count=estimate_tokens(text_value),
    )


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过隔离验收：{exc}")
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tenants WHERE id = ANY(CAST(:ids AS text[]))"),
            {"ids": [TENANT_A, TENANT_B]},
        )
    db_engine.dispose()


def _seed_tenant(engine, tenant_id: str, kb_id: str, rows: list[tuple[str, str]]) -> None:
    with tenant_scope(engine, tenant_id, write=True) as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, :t) "
                 "ON CONFLICT (id) DO NOTHING"),
            {"t": tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, :kb, '', 'active') ON CONFLICT (id) DO NOTHING"
            ),
            {"kb": kb_id, "t": tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:doc, :t, :kb, '隔离验收', '', :hash, 'ready', '{}'::jsonb) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"doc": f"doc_{kb_id}", "t": tenant_id, "kb": kb_id, "hash": f"h_{kb_id}"},
        )


@pytest.fixture(scope="module")
def seeded(engine):
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tenants WHERE id = ANY(CAST(:ids AS text[]))"),
            {"ids": [TENANT_A, TENANT_B]},
        )

    # A 租户：两个知识库
    _seed_tenant(engine, TENANT_A, KB_A1, [])
    _seed_tenant(engine, TENANT_A, KB_A2, [])
    # B 租户：一个知识库
    _seed_tenant(engine, TENANT_B, KB_B1, [])

    kb_a1 = PgVectorStore(engine, tenant_id=TENANT_A, kb_id=KB_A1, embed_model_version="test")
    kb_a2 = PgVectorStore(engine, tenant_id=TENANT_A, kb_id=KB_A2, embed_model_version="test")
    kb_b1 = PgVectorStore(engine, tenant_id=TENANT_B, kb_id=KB_B1, embed_model_version="test")

    a1_chunks = [
        _chunk(f"{KB_A1}#pub", f"doc_{KB_A1}", TEXTS["pub"][0]),
        _chunk(f"{KB_A1}#legal", f"doc_{KB_A1}", TEXTS["legal"][0]),
        _chunk(f"{KB_A1}#finance", f"doc_{KB_A1}", TEXTS["finance"][0]),
    ]
    kb_a1.upsert(a1_chunks, [_vec(1), _vec(2), _vec(3)])
    kb_a2.upsert([_chunk(f"{KB_A2}#1", f"doc_{KB_A2}", TEXTS["a2"][0])], [_vec(4)])
    kb_b1.upsert([_chunk(f"{KB_B1}#1", f"doc_{KB_B1}", TEXTS["b"][0])], [_vec(5)])

    set_chunk_acl(engine, TENANT_A, [f"{KB_A1}#legal"], TEXTS["legal"][1])
    set_chunk_acl(engine, TENANT_A, [f"{KB_A1}#finance"], TEXTS["finance"][1])

    for user_id, tags, role in (
        ("u_member", ["dept:legal"], "member"),
        ("u_leader", ["dept:legal", "dept:finance"], "member"),
        ("u_admin", [], "admin"),
        ("u_visitor", [], "member"),
    ):
        with tenant_scope(engine, TENANT_A, write=True) as connection:
            connection.execute(
                text(
                    "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                    "VALUES (:u, :t, :u, CAST(:tags AS text[]), :role) "
                    "ON CONFLICT (user_id) DO UPDATE SET tags = EXCLUDED.tags, role = EXCLUDED.role"
                ),
                {"u": user_id, "t": TENANT_A, "tags": tags, "role": role},
            )

    return {"a1": kb_a1, "a2": kb_a2, "b": kb_b1}


def _ids(hits) -> set[str]:
    return {hit.chunk_id for hit in hits}


# ---------------------------------------------------------------- 1. 跨租户


def test_cross_tenant_is_blocked(seeded, engine) -> None:
    b_hits = seeded["b"].search(_vec(1), top_k=10, filters={"is_admin": True})

    assert _ids(b_hits) == {f"{KB_B1}#1"}
    assert not any(chunk_id.startswith(KB_A1) for chunk_id in _ids(b_hits))

    # RLS 兜底：即使有人绕过应用层，未声明作用域也查不到 A 的数据
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM chunks")).scalar_one()
    assert count == 0


# ---------------------------------------------------------------- 2. 跨知识库


def test_cross_kb_is_blocked(seeded) -> None:
    hits = seeded["a2"].search(_vec(1), top_k=10, filters={"is_admin": True})

    assert _ids(hits) == {f"{KB_A2}#1"}


# ---------------------------------------------------------------- 3. ACL 越权


def test_acl_denies_unrelated_department(seeded) -> None:
    visitor = seeded["a1"].search(_vec(1), top_k=10, filters={"user_tags": []})

    assert _ids(visitor) == {f"{KB_A1}#pub"}


def test_acl_allows_own_department(seeded) -> None:
    member = seeded["a1"].search(
        _vec(1), top_k=10, filters={"user_tags": ["dept:legal"]}
    )
    leader = seeded["a1"].search(
        _vec(1), top_k=10, filters={"user_tags": ["dept:legal", "dept:finance"]}
    )

    assert _ids(member) == {f"{KB_A1}#pub", f"{KB_A1}#legal"}
    assert _ids(leader) == {f"{KB_A1}#pub", f"{KB_A1}#legal", f"{KB_A1}#finance"}


# ---------------------------------------------------------------- 4. 管理员边界


def test_admin_sees_all_content_but_not_other_tenant(seeded) -> None:
    admin_hits = seeded["a1"].search(_vec(1), top_k=10, filters={"is_admin": True})
    other_tenant = PgVectorStore(seeded["a1"]._engine, tenant_id=TENANT_B, kb_id=KB_A1)
    cross = other_tenant.search(_vec(1), top_k=10, filters={"is_admin": True})

    assert _ids(admin_hits) == {
        f"{KB_A1}#pub",
        f"{KB_A1}#legal",
        f"{KB_A1}#finance",
    }
    assert cross == []


def test_admin_role_comes_from_database(engine, seeded) -> None:
    tags, is_admin = load_user_context(engine, TENANT_A, "u_admin")
    member_tags, member_admin = load_user_context(engine, TENANT_A, "u_member")

    assert tags == [] and is_admin is True
    assert member_tags == ["dept:legal"] and member_admin is False
