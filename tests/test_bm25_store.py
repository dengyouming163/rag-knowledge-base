"""BM25 快照的序列化与持久化测试。

两类用例：
- 不连库：验证 to_payload/from_payload 往返后检索结果完全一致；
- 连库：验证 rebuild 写入快照、新进程（新实例、空缓存）load 后结果不变。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from src.bm25 import BM25Index
from src.bm25_store import BM25Store, IndexNotBuiltError
from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine

TENANT = "t_bm25"
KB = "kb_bm25_a"
KB2 = "kb_bm25_b"

TEXTS = [
    "退款政策：支持七天无理由退款，1-3 个工作日到账。",
    "换货政策：签收后七天内可申请，商品需保持完好。",
    "配送时效：现货订单 48 小时内发出，偏远地区顺延。",
    "发票：支持开具电子发票，3 个工作日内发送到邮箱。",
]


def _chunks(kb_id: str = KB, prefix: str = "bm") -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"{kb_id}#{index + 1}",
            doc_id=f"doc_{prefix}",
            text=text_value,
            title="售后手册",
            section_path=["售后", f"第{index + 1}节"],
            source_url="https://example.invalid/docs/bm25",
            content_hash=f"hash_{prefix}_{index}",
            audience="public",
            token_count=estimate_tokens(text_value),
        )
        for index, text_value in enumerate(TEXTS)
    ]


# ---------------------------------------------------------------- 不连库


def test_payload_roundtrip_keeps_results() -> None:
    original = BM25Index(_chunks())
    restored = BM25Index.from_payload(json.loads(json.dumps(original.to_payload())))

    for query in ["退款", "发票 电子", "配送时效 48", "不存在的词"]:
        assert restored.search(query, top_k=5) == original.search(query, top_k=5)


def test_payload_roundtrip_keeps_avg_len() -> None:
    original = BM25Index(_chunks())
    restored = BM25Index.from_payload(original.to_payload())
    assert restored._avg_len == pytest.approx(original._avg_len)
    assert len(restored) == len(original)


# ---------------------------------------------------------------- 连库


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过集成测试：{exc}")

    with db_engine.begin() as connection:
        # chunks 开了 RLS：写数据前先声明租户作用域
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        _cleanup(connection)
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'bm25')"), {"t": TENANT}
        )
        for kb_id, prefix in ((KB, "bm"), (KB2, "bm2")):
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, 'bm25 kb', '', 'active')"
                ),
                {"kb": kb_id, "t": TENANT},
            )
            connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:doc, :t, :kb, '售后手册', '', :hash, 'ready', '{}'::jsonb)"
                ),
                {"doc": f"doc_{prefix}", "t": TENANT, "kb": kb_id, "hash": f"doc_hash_{kb_id}"},
            )

        for kb_id, prefix in ((KB, "bm"), (KB2, "bm2")):
            for chunk in _chunks(kb_id, prefix):
                connection.execute(
                    text(
                        "INSERT INTO chunks "
                        "(chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                        " embed_model_version, meta) "
                        "VALUES (:chunk_id, :t, :kb, :doc_id, :hash, :text, :tokens, "
                        "        'pending', CAST(:meta AS jsonb))"
                    ),
                    {
                        "chunk_id": chunk.chunk_id,
                        "t": TENANT,
                        "kb": kb_id,
                        "doc_id": chunk.doc_id,
                        "hash": chunk.content_hash,
                        "text": chunk.text,
                        "tokens": chunk.token_count,
                        "meta": json.dumps(
                            {
                                "title": chunk.title,
                                "section_path": chunk.section_path,
                                "source_url": chunk.source_url,
                                "audience": chunk.audience,
                            },
                            ensure_ascii=False,
                        ),
                    },
                )

    yield db_engine

    with db_engine.begin() as connection:
        _cleanup(connection)
    db_engine.dispose()


def _cleanup(connection) -> None:
    connection.execute(text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT})
    connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT})
    connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT})
    connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
    connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def test_rebuild_then_load_matches_memory_index(engine) -> None:
    store = BM25Store(engine)
    store.rebuild(TENANT, KB)

    loaded = store.load(KB)
    in_memory = BM25Index(_chunks())

    assert loaded.search("退款", top_k=4) == in_memory.search("退款", top_k=4)
    assert loaded.search("发票 电子", top_k=3) == in_memory.search("发票 电子", top_k=3)


def test_load_survives_new_process(engine) -> None:
    """模拟重启：新实例、空缓存，结果必须与重建前完全一致（top-10 逐条相同）。"""
    BM25Store(engine).rebuild(TENANT, KB)
    before = BM25Store(engine).load(KB).search("退款 到账", top_k=10)

    # 全新实例 = 全新进程的内存状态
    after = BM25Store(engine).load(KB).search("退款 到账", top_k=10)

    assert [chunk_id for chunk_id, _ in after] == [chunk_id for chunk_id, _ in before]
    assert [score for _, score in after] == [score for _, score in before]


def test_rebuild_bumps_version(engine) -> None:
    store = BM25Store(engine)
    first = store.rebuild(TENANT, KB)
    second = store.rebuild(TENANT, KB)
    assert second == first + 1
    assert store.version(KB) == second


def test_lru_evicts_oldest(engine) -> None:
    store = BM25Store(engine, cache_size=1)
    store.rebuild(TENANT, KB)
    store.rebuild(TENANT, KB2)

    store.load(KB)
    store.load(KB2)
    assert store.cached_kbs == [KB2]


def test_load_before_build_raises(engine) -> None:
    with pytest.raises(IndexNotBuiltError):
        BM25Store(engine).load("kb_never_built")
