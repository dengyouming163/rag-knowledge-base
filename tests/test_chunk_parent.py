"""Task 2.1：父子分块的单元与集成测试。

核心断言：
1. 每个子块都指向一个存在的父块，且父块不参与召回；
2. 子块正文（去掉重叠前缀后）能在父块里找到——保证"补上下文不丢信息"；
3. 向量检索与 BM25 都只看得到子块。
"""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from src.bm25_store import BM25Store
from src.chunk import split_document
from src.db.base import create_db_engine
from src.store_pg import PgVectorStore

DOC = """# 售后政策

本文说明退款与换货规则。

## 退款

支持七天无理由退款。退款审核通过后按原支付路径退回，通常 1-3 个工作日到账。

## 换货

收到货七天内可申请换货。换货需要商品完好、配件齐全。
"""

DIM = 1024
TENANT = "t_parent"
KB = "kb_parent"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def test_structure_has_parent_and_children() -> None:
    chunks = split_document(DOC, doc_id="kb-after-sale")
    parents = [chunk for chunk in chunks if chunk.is_parent]
    children = [chunk for chunk in chunks if not chunk.is_parent]

    assert parents, "至少应生成一个父块"
    assert len(children) >= 2, "两个小节应产生至少两个子块"

    parent_ids = {chunk.chunk_id for chunk in parents}
    assert all(chunk.chunk_id.endswith(("#p1", "#p2")) for chunk in parents) or len(parent_ids) == len(parents)
    for child in children:
        assert child.parent_id in parent_ids, f"{child.chunk_id} 的父块不存在"


def test_child_body_is_inside_parent() -> None:
    chunks = split_document(DOC, doc_id="kb-after-sale")
    by_id = {chunk.chunk_id: chunk for chunk in chunks}

    for child in chunks:
        if child.is_parent:
            continue
        parent_text = _norm(by_id[child.parent_id].text)
        assert _norm(child.text) in parent_text, f"{child.chunk_id} 的正文不在父块中"


def test_parent_is_truncated_to_budget() -> None:
    long_doc = "# 长文\n\n" + "\n\n".join(f"第{i}段内容，用于测试父块上限。" for i in range(200))
    chunks = split_document(long_doc, doc_id="doc-long", parent_max_tokens=200)
    parents = [chunk for chunk in chunks if chunk.is_parent]
    assert parents
    assert all(chunk.token_count <= 260 for chunk in parents)


@pytest.fixture()
def db_engine():
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(engine)
    yield engine
    _cleanup(engine)
    engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def _seed(engine) -> list:
    chunks = split_document(DOC, doc_id="kb-parent")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'parent')"), {"t": TENANT})
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'parent kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('kb-parent', :t, :kb, '售后政策', '', 'doc_hash_parent', 'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )

    store = PgVectorStore(engine, tenant_id=TENANT, kb_id=KB)
    vectors = [
        None if chunk.is_parent else [0.01 * ((i % 7) + 1) for i in range(DIM)]
        for chunk in chunks
    ]
    store.upsert(chunks, vectors)
    return chunks


def test_store_persists_parent_without_embedding(db_engine) -> None:
    chunks = _seed(db_engine)
    parents = [chunk.chunk_id for chunk in chunks if chunk.is_parent]

    with db_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT chunk_id, embedding IS NULL AS no_vec FROM chunks "
                "WHERE tenant_id = :t AND is_parent = true"
            ),
            {"t": TENANT},
        ).mappings().all()

    assert {row["chunk_id"] for row in rows} == set(parents)
    assert all(row["no_vec"] for row in rows)


def test_vector_search_returns_children_only(db_engine) -> None:
    chunks = _seed(db_engine)
    child_ids = {chunk.chunk_id for chunk in chunks if not chunk.is_parent}

    store = PgVectorStore(db_engine, tenant_id=TENANT, kb_id=KB)
    hits = store.search([0.05] * DIM, top_k=10)

    assert hits
    assert {hit.chunk_id for hit in hits} <= child_ids
    # 命中结果要带上 parent_id，供上层补全上下文
    assert all(hit.metadata.get("parent_id") for hit in hits)


def test_bm25_index_excludes_parents(db_engine) -> None:
    chunks = _seed(db_engine)
    child_ids = {chunk.chunk_id for chunk in chunks if not chunk.is_parent}

    index = BM25Store(db_engine)
    results = index.rebuild(TENANT, KB)

    assert results > 0
    with db_engine.connect() as connection:
        payload = connection.execute(  # JSONB 经 SQLAlchemy 已反序列化为 dict
            text("SELECT payload FROM kb_bm25 WHERE tenant_id = :t AND kb_id = :kb"),
            {"t": TENANT, "kb": KB},
        ).scalar_one()

    stored_ids = set(payload["chunks"]) if isinstance(payload["chunks"], dict) else {
        item["chunk_id"] for item in payload["chunks"]
    }
    assert stored_ids == child_ids
