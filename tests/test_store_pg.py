"""PgVectorStore 契约测试：同一批数据，两个 store 必须给出相同顺序。

分两层：
- 不连库的用例：验证缺少租户/知识库过滤时立即抛错（这是安全底线，必须永远能跑）。
- 连库的用例：拿 LocalVectorStore 做基准，逐条比对 chunk_id 顺序。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine
from src.store import LocalVectorStore
from src.store_pg import MissingTenantFilterError, PgVectorStore

DIM = 1024
TENANT = "t_contract"
OTHER_TENANT = "t_other"
KB = "kb_contract"


def _random_vectors(count: int, seed: int = 7) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.uniform(-1.0, 1.0) for _ in range(DIM)] for _ in range(count)]


def _make_chunks() -> list[Chunk]:
    texts = [
        "退款政策：支持七天无理由退款，1-3 个工作日到账。",
        "换货政策：签收后七天内可申请，商品需保持完好。",
        "配送时效：现货订单 48 小时内发出。",
        "修改地址：未发货订单可在详情页自助修改。",
        "优惠券：满 199 减 30，不与会员折扣叠加。",
        "发票：支持开具电子发票，3 个工作日内发送。",
    ]
    return [
        Chunk(
            chunk_id=f"{KB}#{index + 1}",
            doc_id="doc_c1" if index < 3 else "doc_c2",
            text=text_value,
            title="售后手册",
            section_path=["售后", f"第{index + 1}节"],
            source_url="https://example.invalid/docs/contract",
            content_hash=f"hash_{index + 1}",
            audience="public",
            token_count=estimate_tokens(text_value),
        )
        for index, text_value in enumerate(texts)
    ]


# ---------------------------------------------------------------- 不连库用例


def test_search_requires_tenant_id() -> None:
    store = PgVectorStore(None, kb_id=KB)  # type: ignore[arg-type]
    with pytest.raises(MissingTenantFilterError):
        store.search([0.0] * DIM, top_k=5)


def test_search_requires_kb_id() -> None:
    store = PgVectorStore(None, tenant_id=TENANT)  # type: ignore[arg-type]
    with pytest.raises(MissingTenantFilterError):
        store.search([0.0] * DIM, top_k=5)


def test_empty_kb_list_is_rejected() -> None:
    store = PgVectorStore(None, tenant_id=TENANT)  # type: ignore[arg-type]
    with pytest.raises(MissingTenantFilterError):
        store.search([0.0] * DIM, top_k=5, filters={"kb_id": []})


# ---------------------------------------------------------------- 连库用例


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
        connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": TENANT, "b": OTHER_TENANT})
    db_engine.dispose()


@pytest.fixture()
def seeded(engine):
    """准备租户、知识库、文档与另一个租户的干扰数据，用完整回滚保持隔离。"""
    chunks = _make_chunks()
    vectors = _random_vectors(len(chunks))

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": TENANT, "b": OTHER_TENANT})
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'contract'), (:o, 'other')"), {"t": TENANT, "o": OTHER_TENANT})
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'contract kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_c1', :t, :kb, '售后手册', '', 'doc_hash_1', 'active', '{}'::jsonb), "
                "('doc_c2', :t, :kb, '售后手册', '', 'doc_hash_2', 'active', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )

    store = PgVectorStore(engine, tenant_id=TENANT, kb_id=KB, embed_model_version="test")
    store.upsert(chunks, vectors)

    yield chunks, vectors, store


def test_contract_same_order_as_local_store(seeded, tmp_path) -> None:
    chunks, vectors, pg_store = seeded
    local_store = LocalVectorStore(tmp_path)
    local_store.upsert(chunks, vectors)

    query_vector = vectors[0]
    pg_hits = pg_store.search(query_vector, top_k=5, filters={"audience": "public"})
    local_hits = local_store.search(query_vector, top_k=5, filters={"audience": "public"})

    assert [hit.chunk_id for hit in pg_hits] == [hit.chunk_id for hit in local_hits]
    # 余弦相似度的值也应接近（顺序最重要，分数允许浮点误差）
    for pg_hit, local_hit in zip(pg_hits, local_hits):
        assert pg_hit.score == pytest.approx(local_hit.score, abs=1e-6)


def test_tenant_isolation(engine, seeded) -> None:
    """另一个租户写入同名向量后，当前租户检索不得看到它。"""
    _, vectors, pg_store = seeded
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES ('kb_other', :t, 'other kb', '', 'active')"
            ),
            {"t": OTHER_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_other', :t, 'kb_other', 'other', '', 'doc_hash_other', 'active', '{}'::jsonb)"
            ),
            {"t": OTHER_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                "embedding, embed_model_version, meta) "
                "VALUES ('other#1', :t, 'kb_other', 'doc_other', 'hash_other', '干扰数据', 1, "
                "CAST(:vec AS vector), 'test', '{\"audience\": \"public\"}'::jsonb)"
            ),
            {"t": OTHER_TENANT, "vec": "[" + ",".join(repr(value) for value in vectors[0]) + "]"},
        )

    hits = pg_store.search(vectors[0], top_k=10)
    assert all(hit.chunk_id != "other#1" for hit in hits)
    assert len(hits) == 6


def test_wrong_query_dimension_rejected(seeded) -> None:
    _, _, pg_store = seeded
    with pytest.raises(DBAPIError):
        pg_store.search([0.1, 0.2, 0.3], top_k=5)


def test_delete_by_doc_and_count(seeded) -> None:
    _, _, pg_store = seeded
    assert pg_store.count() == 6
    assert pg_store.delete_by_doc("doc_c2") == 3
    assert pg_store.count() == 3
