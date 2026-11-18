"""src/retrieve.py 的单元测试：假向量库 + 假 BM25 索引，完全离线。"""

from __future__ import annotations

import hashlib

import pytest

from src.bm25 import BM25Index
from src.chunk import Chunk
from src.retrieve import RRF_K, HybridRetriever
from src.store import LocalVectorStore, SearchHit


def make_chunk(
    chunk_id: str,
    text: str,
    *,
    doc_id: str = "doc1",
    audience: str = "public",
    section_path: list[str] | None = None,
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        text=text,
        title="售后政策",
        section_path=section_path or ["售后政策"],
        source_url="https://example.invalid/doc",
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        audience=audience,
        token_count=10,
    )


def make_hit(chunk_id: str, score: float, *, text: str = "文本", audience: str = "public") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        text=text,
        score=score,
        metadata={"audience": audience, "title": "售后政策", "section_path": ["售后政策"]},
    )


class FakeEmbedClient:
    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._table[text] for text in texts]


class FakeVectorStore:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits
        self.calls: list[dict] = []

    def search(self, query_vector: list[float], top_k: int = 10, filters: dict | None = None) -> list[SearchHit]:
        self.calls.append({"vector": list(query_vector), "top_k": top_k, "filters": filters})
        return list(self._hits)


class FakeBM25Index:
    def __init__(self, results: list[tuple[str, float]], chunks: list[Chunk]) -> None:
        self._results = results
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self.calls: list[dict] = []

    def search(self, query: str, top_k: int = 10, filters: dict | None = None) -> list[tuple[str, float]]:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        return list(self._results)[:top_k]

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)


QUERY_VECTOR = [1.0, 0.0]


def test_rrf_fuses_two_routes_by_rank():
    chunks = [make_chunk("doc1#1", "退款到账"), make_chunk("doc1#2", "退款说明"), make_chunk("doc1#3", "物流时效")]
    store = FakeVectorStore([make_hit("doc1#2", 0.9), make_hit("doc1#1", 0.8)])
    bm25 = FakeBM25Index([("doc1#1", 3.2), ("doc1#3", 1.0)], chunks)
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    hits = retriever.retrieve("退款", top_k=3)

    assert [hit.chunk_id for hit in hits] == ["doc1#1", "doc1#2", "doc1#3"]
    assert hits[0].score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert hits[1].score == pytest.approx(1 / (RRF_K + 1))
    assert hits[2].score == pytest.approx(1 / (RRF_K + 2))
    assert hits[0].score > hits[1].score > hits[2].score


def test_hybrid_keeps_vector_metadata_and_backfills_bm25_only_hits():
    chunks = [
        make_chunk("doc1#1", "退款到账", section_path=["售后政策", "退款"]),
        make_chunk("doc1#3", "物流时效", section_path=["物流", "时效"]),
    ]
    store = FakeVectorStore([make_hit("doc1#1", 0.9)])
    bm25 = FakeBM25Index([("doc1#1", 2.0), ("doc1#3", 1.0)], chunks)
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    hits = {hit.chunk_id: hit for hit in retriever.retrieve("退款", top_k=5)}

    assert hits["doc1#3"].text == "物流时效"
    assert hits["doc1#3"].metadata["section_path"] == ["物流", "时效"]
    assert hits["doc1#3"].doc_id == "doc1"


def test_duplicate_chunk_appears_once_with_summed_score():
    chunks = [make_chunk("doc1#1", "退款到账"), make_chunk("doc1#2", "退款说明")]
    store = FakeVectorStore([make_hit("doc1#1", 0.9)])
    bm25 = FakeBM25Index([("doc1#1", 5.0), ("doc1#2", 1.0)], chunks)
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    hits = retriever.retrieve("退款", top_k=5)

    ids = [hit.chunk_id for hit in hits]
    assert ids.count("doc1#1") == 1
    assert hits[0].chunk_id == "doc1#1"
    assert hits[0].score == pytest.approx(2 / (RRF_K + 1))
    assert hits[0].score > hits[1].score


def test_ties_are_broken_by_chunk_id_ascending():
    chunks = [make_chunk("doc#a", "退款到账"), make_chunk("doc#b", "退款说明")]
    store = FakeVectorStore([make_hit("doc#b", 0.9), make_hit("doc#a", 0.8)])
    bm25 = FakeBM25Index([("doc#a", 2.0), ("doc#b", 1.0)], chunks)
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    hits = retriever.retrieve("退款", top_k=5)

    assert hits[0].score == pytest.approx(hits[1].score)
    assert [hit.chunk_id for hit in hits] == ["doc#a", "doc#b"]


def test_single_route_modes_return_raw_scores():
    store = FakeVectorStore([make_hit("doc1#1", 0.9), make_hit("doc1#2", 0.8)])
    bm25 = FakeBM25Index([("doc1#2", 4.0), ("doc1#1", 1.5)], [make_chunk("doc1#1", "a"), make_chunk("doc1#2", "b")])
    embed = FakeEmbedClient({"退款": QUERY_VECTOR})
    retriever = HybridRetriever(store, bm25, embed)

    vector_hits = retriever.retrieve("退款", top_k=1, mode="vector")
    bm25_hits = retriever.retrieve("退款", top_k=1, mode="bm25")

    assert [(hit.chunk_id, hit.score) for hit in vector_hits] == [("doc1#1", 0.9)]
    assert [(hit.chunk_id, hit.score) for hit in bm25_hits] == [("doc1#2", 4.0)]
    assert len(embed.calls) == 1
    assert len(bm25.calls) == 1


def test_filters_and_recall_k_reach_both_routes():
    store = FakeVectorStore([make_hit("doc1#1", 0.9, audience="internal")])
    bm25 = FakeBM25Index([("doc1#1", 2.0)], [make_chunk("doc1#1", "退款", audience="internal")])
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    retriever.retrieve("退款", top_k=5, recall_k=7, filters={"audience": "internal"})

    assert store.calls[0]["top_k"] == 7
    assert store.calls[0]["filters"] == {"audience": "internal"}
    assert bm25.calls[0]["top_k"] == 7
    assert bm25.calls[0]["filters"] == {"audience": "internal"}


def test_empty_routes_yield_no_hits():
    retriever = HybridRetriever(
        FakeVectorStore([]),
        FakeBM25Index([], []),
        FakeEmbedClient({"无关查询": QUERY_VECTOR}),
    )

    assert retriever.retrieve("无关查询") == []


def test_unknown_mode_raises():
    retriever = HybridRetriever(
        FakeVectorStore([]),
        FakeBM25Index([], []),
        FakeEmbedClient({}),
    )

    with pytest.raises(ValueError):
        retriever.retrieve("退款", mode="dense")  # type: ignore[arg-type]


def test_hybrid_with_real_index_and_store_respects_filters(tmp_path):
    store = LocalVectorStore(tmp_path)
    chunks = [
        make_chunk("doc1#1", "对外口径：退款三个工作日到账", audience="public"),
        make_chunk("doc1#2", "内部口径：退款实际 T+1 到账", audience="internal"),
        make_chunk("doc1#3", "物流时效说明", audience="public"),
    ]
    store.upsert(chunks, [[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]])
    retriever = HybridRetriever(
        store, BM25Index(chunks), FakeEmbedClient({"退款": QUERY_VECTOR})
    )

    hits = retriever.retrieve("退款", filters={"audience": "internal"})

    assert [hit.chunk_id for hit in hits] == ["doc1#2"]
    assert hits[0].metadata["audience"] == "internal"


def test_compare_routes_reports_three_modes():
    chunks = [
        make_chunk(f"doc1#{index}", f"退款说明 {index}") for index in range(1, 8)
    ]
    store = FakeVectorStore([make_hit(f"doc1#{index}", 1.0 - index * 0.1) for index in range(1, 8)])
    bm25 = FakeBM25Index([(f"doc1#{index}", 10.0 - index) for index in range(1, 8)], chunks)
    retriever = HybridRetriever(store, bm25, FakeEmbedClient({"退款": QUERY_VECTOR}))

    report = retriever.compare_routes("退款")

    assert list(report) == ["vector", "bm25", "hybrid"]
    for rows in report.values():
        assert len(rows) == 5
        assert all(isinstance(chunk_id, str) and isinstance(score, float) for chunk_id, score in rows)
    assert report["vector"][0][0] == "doc1#1"
    assert report["bm25"][0][0] == "doc1#1"
