"""src/store.py 的单元测试：使用假向量，不依赖网络与外部服务。"""

from __future__ import annotations

import hashlib

import pytest

from src.chunk import Chunk
from src.store import LocalVectorStore


def make_chunk(
    chunk_id: str,
    text: str,
    *,
    doc_id: str = "doc1",
    audience: str = "public",
    title: str = "售后政策",
    section_path: list[str] | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        title=title,
        section_path=section_path or ["售后政策"],
        source_url="https://example.invalid/doc",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        audience=audience,
        token_count=10,
    )


def test_upsert_writes_all_and_counts(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [make_chunk(f"doc1#{i}", f"文本{i}") for i in range(3)]

    written = store.upsert(chunks, [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])

    assert written == 3
    assert store.count() == 3


def test_duplicate_content_hash_is_skipped(tmp_path):
    store = LocalVectorStore(tmp_path)
    first = make_chunk("doc1#1", "重复内容")
    duplicate = make_chunk("doc1#2", "重复内容")
    other = make_chunk("doc1#3", "不同内容")

    assert store.upsert([first], [[1.0, 0.0]]) == 1
    assert store.upsert([duplicate, other], [[1.0, 0.0], [0.0, 1.0]]) == 1
    assert store.count() == 2


def test_length_mismatch_raises(tmp_path):
    store = LocalVectorStore(tmp_path)

    with pytest.raises(ValueError):
        store.upsert([make_chunk("doc1#1", "文本")], [])


def test_search_orders_by_cosine_similarity(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [
        make_chunk("doc1#1", "最相关"),
        make_chunk("doc1#2", "中等相关"),
        make_chunk("doc1#3", "不相关"),
    ]
    store.upsert(chunks, [[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]])

    hits = store.search([1.0, 0.0])

    assert [hit.chunk_id for hit in hits] == ["doc1#1", "doc1#2", "doc1#3"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.6)
    assert hits[2].score == pytest.approx(0.0)


def test_search_respects_top_k(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [make_chunk(f"doc1#{i}", f"文本{i}") for i in range(5)]
    store.upsert(chunks, [[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.7, 0.3], [0.6, 0.4]])

    hits = store.search([1.0, 0.0], top_k=2)

    assert len(hits) == 2
    assert [hit.chunk_id for hit in hits] == ["doc1#0", "doc1#1"]


def test_search_filters_by_audience(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [
        make_chunk("doc1#1", "公开内容", audience="public"),
        make_chunk("doc1#2", "内部内容", audience="internal"),
    ]
    store.upsert(chunks, [[1.0, 0.0], [0.9, 0.1]])

    hits = store.search([1.0, 0.0], filters={"audience": "internal"})

    assert [hit.chunk_id for hit in hits] == ["doc1#2"]
    assert hits[0].metadata["audience"] == "internal"


def test_delete_by_doc_removes_only_that_document(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [
        make_chunk("a#1", "文档 A 第一段", doc_id="a"),
        make_chunk("a#2", "文档 A 第二段", doc_id="a"),
        make_chunk("b#1", "文档 B 第一段", doc_id="b"),
    ]
    store.upsert(chunks, [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])

    deleted = store.delete_by_doc("a")

    assert deleted == 2
    assert store.count() == 1
    assert [hit.doc_id for hit in store.search([0.0, 1.0])] == ["b"]


def test_data_survives_restart(tmp_path):
    chunks = [
        make_chunk("doc1#1", "退款需三个工作日", section_path=["售后政策", "退款"]),
        make_chunk("doc1#2", "换货需七天内申请", section_path=["售后政策", "换货"]),
    ]
    first = LocalVectorStore(tmp_path)
    first.upsert(chunks, [[1.0, 0.0], [0.0, 1.0]])

    reopened = LocalVectorStore(tmp_path)
    hits = reopened.search([1.0, 0.0], top_k=1)

    assert reopened.count() == 2
    assert hits[0].chunk_id == "doc1#1"
    assert hits[0].text == "退款需三个工作日"
    assert hits[0].metadata == {
        "section_path": ["售后政策", "退款"],
        "title": "售后政策",
        "audience": "public",
        "source_url": "https://example.invalid/doc",
    }
