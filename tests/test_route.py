"""Task 2.2：Query 路由与加权 RRF 的测试。

两条底线：
1. 规则必须可预测——同一种写法永远命中同一条路由，不依赖模型；
2. 加权只能改变"名次贡献"，不能把原始分数引进融合（否则量纲问题回来了）。
"""

from __future__ import annotations

import pytest

from src.chunk import Chunk
from src.retrieve import HybridRetriever
from src.route import DEFAULT_ROUTE, IDENTIFIER_ROUTE, detect_route
from src.store import SearchHit


def _hit(chunk_id: str) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="doc", text=f"正文 {chunk_id}", score=1.0, metadata={})


@pytest.mark.parametrize(
    "query",
    ["X200-Pro 的保修期是多久", "E1024 这个错误码怎么处理", "SKU-1001 多少钱", "x200 呢"],
)
def test_identifier_queries_route_to_bm25(query: str) -> None:
    assert detect_route(query) is IDENTIFIER_ROUTE


@pytest.mark.parametrize(
    "query",
    ["退款多久到账", "七天无理由是怎么规定的", "", "   "],
)
def test_plain_queries_use_default_route(query: str) -> None:
    assert detect_route(query) is DEFAULT_ROUTE


def test_default_weights_keep_rrf_order() -> None:
    vector_hits = [_hit("a"), _hit("b")]
    bm25_hits = [_hit("c"), _hit("a")]

    fused = HybridRetriever._fuse(vector_hits, bm25_hits)

    # a 两路都上榜（0.5/61 + 0.5/62）最高；c 在 BM25 第 1（0.5/61）高于 b 在向量第 2（0.5/62）
    assert [hit.chunk_id for hit in fused] == ["a", "c", "b"]
    assert fused[0].score == pytest.approx(0.5 / 61 + 0.5 / 62)


def test_bm25_weight_lifts_bm25_only_hit() -> None:
    vector_hits = [_hit("a"), _hit("b")]
    bm25_hits = [_hit("c"), _hit("a")]

    fused = HybridRetriever._fuse(
        vector_hits, bm25_hits, weights=(0.3, 0.7), route_name="identifier"
    )

    # c（BM25 第 1）超过 b（向量第 2）：0.7/61 > 0.3/61
    assert [hit.chunk_id for hit in fused] == ["a", "c", "b"]
    assert fused[1].score == pytest.approx(0.7 / 61)
    assert all(hit.metadata["route"] == "identifier" for hit in fused)


def test_weights_length_must_match_routes() -> None:
    with pytest.raises(ValueError):
        HybridRetriever._fuse([_hit("a")], weights=(0.5, 0.5))


class _FakeVectorStore:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, query_vector, top_k=10, filters=None):
        self.calls += 1
        return [_hit("vec#1")]


class _FakeEmbed:
    def embed_texts(self, texts):
        return [[0.1] * 8 for _ in texts]


class _FakeBM25:
    def search(self, query, top_k=10, filters=None):
        return [("bm25#1", 5.0)]

    def get_chunk(self, chunk_id):
        return Chunk(
            chunk_id=chunk_id,
            doc_id="doc",
            text="正文",
            title="",
            section_path=[],
            source_url="",
            content_hash="h",
            token_count=2,
        )


def test_retriever_records_route_in_metadata() -> None:
    retriever = HybridRetriever(_FakeVectorStore(), _FakeBM25(), _FakeEmbed())

    identifier_hits = retriever.retrieve("X200-Pro 保修多久", top_k=2)
    plain_hits = retriever.retrieve("退款多久到账", top_k=2)

    assert identifier_hits[0].metadata["route"] == "identifier"
    assert plain_hits[0].metadata["route"] == "default"


def test_explicit_route_overrides_detection() -> None:
    retriever = HybridRetriever(_FakeVectorStore(), _FakeBM25(), _FakeEmbed())

    hits = retriever.retrieve("X200-Pro 保修多久", top_k=2, route=DEFAULT_ROUTE)

    assert hits[0].metadata["route"] == "default"
