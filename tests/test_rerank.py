"""Task 2.3：Rerank 适配层测试。

最重要的一条：**精排失败绝不能拖垮问答**。
所以每个异常分支都断言"结果仍然返回 + degraded=True"。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.pipeline import RagPipeline
from src.rerank import ApiReranker, NullReranker, build_reranker_from_env
from src.store import SearchHit


def _hit(chunk_id: str, score: float = 1.0) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="doc", text=f"正文-{chunk_id}", score=score, metadata={})


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200, exc: Exception | None = None):
        self.payload = payload
        self.status_code = status_code
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.exc:
            raise self.exc
        return FakeResponse(self.payload, self.status_code)


def test_null_reranker_keeps_order_and_truncates() -> None:
    hits = [_hit("a"), _hit("b"), _hit("c")]

    result = NullReranker().rerank("退款", hits, top_k=2)

    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]
    assert result.degraded is False


def test_api_reranker_reorders_by_relevance() -> None:
    client = FakeClient(
        {
            "results": [
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0.9},
                {"index": 2, "relevance_score": 0.5},
            ]
        }
    )
    reranker = ApiReranker("https://rerank.invalid", client=client)

    result = reranker.rerank("退款多久到账", [_hit("a"), _hit("b"), _hit("c")], top_k=2)

    assert result.degraded is False
    assert [hit.chunk_id for hit in result.hits] == ["b", "c"]
    assert result.hits[0].score == pytest.approx(0.9)
    assert result.hits[0].metadata["rerank_score"] == pytest.approx(0.9)
    assert client.calls[0]["timeout"] == 0.8
    assert client.calls[0]["json"]["query"] == "退款多久到账"


def test_timeout_degrades_to_null_reranker() -> None:
    client = FakeClient(exc=httpx.TimeoutException("timed out"))
    reranker = ApiReranker("https://rerank.invalid", client=client)

    result = reranker.rerank("退款", [_hit("a"), _hit("b")], top_k=2)

    assert result.degraded is True
    assert "Timeout" in (result.reason or "")
    # 降级时保持粗排顺序，不能因为精排挂了就返回空
    assert [hit.chunk_id for hit in result.hits] == ["a", "b"]


def test_non_200_degrades_instead_of_raising() -> None:
    client = FakeClient({}, status_code=502)
    reranker = ApiReranker("https://rerank.invalid", client=client)

    result = reranker.rerank("退款", [_hit("a")], top_k=1)

    assert result.degraded is True
    assert "502" in (result.reason or "")


def test_malformed_payload_degrades() -> None:
    client = FakeClient({"unexpected": []})
    reranker = ApiReranker("https://rerank.invalid", client=client)

    result = reranker.rerank("退款", [_hit("a")], top_k=1)

    assert result.degraded is True
    assert [hit.chunk_id for hit in result.hits] == ["a"]


def test_empty_hits_skip_remote_call() -> None:
    client = FakeClient({"results": []})
    reranker = ApiReranker("https://rerank.invalid", client=client)

    result = reranker.rerank("退款", [], top_k=5)

    assert result.hits == []
    assert client.calls == []


def test_build_from_env_defaults_to_null(monkeypatch) -> None:
    monkeypatch.delenv("RERANK_BASE_URL", raising=False)
    assert isinstance(build_reranker_from_env(), NullReranker)

    monkeypatch.setenv("RERANK_BASE_URL", "https://rerank.invalid")
    assert isinstance(build_reranker_from_env(), ApiReranker)


class _FakeRetriever:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    def retrieve(self, query, top_k=10, filters=None, mode="hybrid", route=None):
        return list(self._hits)


class _FakeChat:
    def chat(self, messages, temperature=0.0):
        return SimpleNamespace(text="退款通常 1-3 个工作日到账。[b]")


class _ReversingReranker:
    def rerank(self, query, hits, top_k):
        from src.rerank import RerankResult

        return RerankResult(hits=list(reversed(hits))[:top_k], degraded=True, reason="forced")


def test_pipeline_applies_rerank_and_reports_degradation() -> None:
    pipeline = RagPipeline(
        chat_client=_FakeChat(),
        embed_client=None,
        store=None,
        bm25_index=None,
        retriever=_FakeRetriever([_hit("a"), _hit("b")]),
        reranker=_ReversingReranker(),
    )

    result = pipeline.answer("退款多久到账", top_k=2)

    assert result["chunks"] == ["b", "a"]
    assert result["rerank_degraded"] is True


def test_pipeline_without_reranker_is_not_degraded() -> None:
    pipeline = RagPipeline(
        chat_client=_FakeChat(),
        embed_client=None,
        store=None,
        bm25_index=None,
        retriever=_FakeRetriever([_hit("a")]),
    )

    result = pipeline.answer("退款多久到账", top_k=1)

    assert result["rerank_degraded"] is False
