"""src/bm25.py 的单元测试：纯本地计算，不依赖网络与第三方检索库。"""

from __future__ import annotations

import hashlib

import pytest

from src.bm25 import BM25Index, tokenize
from src.chunk import Chunk


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


def test_tokenize_chinese_uses_single_chars_and_bigrams():
    assert tokenize("退款政策") == ["退", "款", "政", "策", "退款", "款政", "政策"]


def test_tokenize_english_lowercases_and_splits_words():
    assert tokenize("Refund Policy 2025") == ["refund", "policy", "2025"]


def test_tokenize_mixes_chinese_and_english():
    tokens = tokenize("iPhone 15 退款")

    assert "iphone" in tokens
    assert "15" in tokens
    assert "退款" in tokens
    assert "iPhone" not in tokens


def test_search_ranks_higher_overlap_first():
    chunks = [
        make_chunk("doc1#1", "退款政策：七天无理由退款，退款三个工作日到账"),
        make_chunk("doc1#2", "退款说明：仅支持原路退款"),
        make_chunk("doc1#3", "物流时效说明：偏远地区延迟"),
    ]
    index = BM25Index(chunks)

    hits = index.search("退款 到账")

    assert [chunk_id for chunk_id, _ in hits] == ["doc1#1", "doc1#2"]
    assert hits[0][1] > hits[1][1] > 0


def test_search_returns_empty_for_blank_query():
    index = BM25Index([make_chunk("doc1#1", "退款三个工作日到账")])

    assert index.search("") == []
    assert index.search("   ") == []


def test_search_returns_empty_when_nothing_matches():
    index = BM25Index([make_chunk("doc1#1", "退款三个工作日到账")])

    assert index.search("量子计算") == []


def test_search_filters_by_audience():
    chunks = [
        make_chunk("doc1#1", "对外口径：退款三个工作日到账", audience="public"),
        make_chunk("doc1#2", "内部口径：退款实际 T+1 到账", audience="internal"),
    ]
    index = BM25Index(chunks)

    hits = index.search("退款", filters={"audience": "internal"})

    assert [chunk_id for chunk_id, _ in hits] == ["doc1#2"]


def test_get_chunk_returns_original_chunk():
    chunk = make_chunk("doc1#1", "退款三个工作日到账", section_path=["售后政策", "退款"])
    index = BM25Index([chunk])

    assert index.get_chunk("doc1#1").text == "退款三个工作日到账"
    assert index.get_chunk("doc1#404") is None


def test_invalid_parameters_raise():
    chunks = [make_chunk("doc1#1", "文本")]

    with pytest.raises(ValueError):
        BM25Index(chunks, k1=0)
    with pytest.raises(ValueError):
        BM25Index(chunks, b=1.5)
