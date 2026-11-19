"""Task 3.4：版本发布与回滚。

三个必须成立的事实：
1. 发布后 active 指针指向新版本，旧版本被标记 superseded；
2. 同库同一时刻只有一个 published 版本（数据库唯一索引兜底）；
3. 回滚后按该版本的 chunk_ids 检索，新增的块不可见。
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy import text

from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine
from src.db.session import tenant_scope
from src.store_pg import PgVectorStore
from src.versioning import (
    VersionNotFoundError,
    active_version_chunk_ids,
    publish,
    rollback,
)

DIM = 1024
TENANT = "t_ver"
KB = "kb_ver"


def _vec(seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(DIM)]


def _chunk(name: str, text_value: str) -> Chunk:
    return Chunk(
        chunk_id=f"{KB}#{name}",
        doc_id=f"doc_{KB}",
        text=text_value,
        title="版本测试",
        section_path=["版本"],
        source_url="",
        content_hash=f"ver_{name}",
        token_count=estimate_tokens(text_value),
    )


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过版本测试：{exc}")
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    db_engine.dispose()


@pytest.fixture()
def seeded(engine):
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'ver')"), {"t": TENANT})
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'ver kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('doc_kb_ver', :t, :kb, '版本测试', '', 'h_ver', 'ready', '{}'::jsonb)"
            ),
            {"t": TENANT, "kb": KB},
        )

    store = PgVectorStore(engine, tenant_id=TENANT, kb_id=KB, embed_model_version="test")
    store.upsert([_chunk("1", "退款政策第一版"), _chunk("2", "换货政策第一版")], [_vec(1), _vec(2)])
    return store


def test_publish_sets_active_and_supersedes(engine, seeded) -> None:
    v1 = publish(engine, TENANT, KB, note="首次发布")

    assert v1["version"] == 1 and v1["chunk_count"] == 2
    with tenant_scope(engine, TENANT) as connection:
        active = connection.execute(
            text("SELECT active_version_id FROM knowledge_bases WHERE id = :kb"), {"kb": KB}
        ).scalar_one()
    assert active == v1["version_id"]

    v2 = publish(engine, TENANT, KB, note="第二次发布")
    with tenant_scope(engine, TENANT) as connection:
        rows = connection.execute(
            text("SELECT version, status FROM kb_versions WHERE kb_id = :kb ORDER BY version"),
            {"kb": KB},
        ).all()

    assert v2["version"] == 2
    assert [(version, status) for version, status in rows] == [
        (1, "superseded"),
        (2, "published"),
    ]


def test_rollback_pins_retrieval_to_old_snapshot(engine, seeded) -> None:
    v1 = publish(engine, TENANT, KB)
    v1_ids = active_version_chunk_ids(engine, TENANT, KB)

    # 发布后新增一条，再发第二版
    seeded.upsert([_chunk("3", "新增的维修政策")], [_vec(3)])
    v2 = publish(engine, TENANT, KB)
    assert v2["chunk_count"] == 3

    # 回滚到 v1：可见集合恢复为 2 条
    restored = rollback(engine, TENANT, KB, v1["version_id"])
    assert restored["chunk_count"] == 2

    pinned = seeded.search(
        _vec(1), top_k=10, filters={"version_chunk_ids": restored["chunk_ids"]}
    )
    assert {hit.chunk_id for hit in pinned} == set(v1_ids)
    assert f"{KB}#3" not in {hit.chunk_id for hit in pinned}

    latest = seeded.search(_vec(1), top_k=10)
    assert f"{KB}#3" in {hit.chunk_id for hit in latest}


def test_rollback_rejects_unknown_version(engine, seeded) -> None:
    publish(engine, TENANT, KB)

    with pytest.raises(VersionNotFoundError):
        rollback(engine, TENANT, KB, 999999)


def test_publish_empty_kb_raises(engine, seeded) -> None:
    with tenant_scope(engine, TENANT, write=True) as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES ('kb_ver_empty', :t, 'empty', '', 'active')"
            ),
            {"t": TENANT},
        )

    with pytest.raises(ValueError):
        publish(engine, TENANT, "kb_ver_empty")
