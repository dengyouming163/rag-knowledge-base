"""Search 终态 RAG 指标与 retrieve 阶段跨度。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import Request, Response

from app.routers import search as search_module
from app.schemas import SearchRequest, SearchResponse
from src import ops_observability


TENANT = "tenant-from-dependency"
KB = "kb-search"
ENGINE = object()
HIT = SimpleNamespace(
    chunk_id="chunk-1",
    doc_id="doc-1",
    score=0.123456789,
    text="refund policy",
    metadata={"audience": "public"},
)


class TraceStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


class Retriever:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def retrieve(self, *args, **kwargs):
        if self.error is not None:
            raise self.error
        return self.result


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/v1/kbs/{KB}/search",
            "headers": [],
        }
    )


def install_dependencies(monkeypatch, retriever: Retriever) -> TraceStore:
    monkeypatch.setattr(search_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(search_module, "user_context_of", lambda *args: (["tag"], False))
    monkeypatch.setattr(search_module, "build_retriever", lambda *args, **kwargs: retriever)
    return TraceStore()


def invoke_search(request: Request, response: Response, trace_store: TraceStore):
    return search_module.search(
        kb_id=KB,
        payload=SearchRequest(query="refund", mode="hybrid", top_k=3),
        request=request,
        response=response,
        tenant_id=TENANT,
        user_id="user-1",
        engine=ENGINE,
        embed_client=object(),
        trace_store=trace_store,
    )


def capture_metrics(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def capture(engine, **fields) -> None:
        assert engine is ENGINE
        calls.append(fields)

    monkeypatch.setattr(search_module, "try_record_rag_metric", capture, raising=False)
    return calls


def assert_request_state(request: Request, response: Response, metric: dict) -> None:
    assert request.state.kb_id == KB
    assert request.state.trace_id == response.headers["X-Trace-Id"]
    assert metric["trace_id"] == request.state.trace_id


def test_search_success_records_one_terminal_metric(monkeypatch) -> None:
    calls = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch, Retriever(result=[HIT]))
    request = make_request()
    response = Response()

    result = invoke_search(request, response, store)

    assert isinstance(result, SearchResponse)
    assert result.kb_id == KB
    assert len(result.hits) == 1
    assert result.hits[0].chunk_id == HIT.chunk_id
    assert result.hits[0].score == 0.123457
    assert len(calls) == 1
    metric = calls[0]
    assert metric["tenant_id"] == TENANT
    assert metric["kb_id"] == KB
    assert metric["operation"] == "search"
    assert metric["mode"] == "hybrid"
    assert metric["outcome"] == "succeeded"
    assert metric["hits"] == 1
    assert metric["total_ms"] >= metric["retrieval_ms"] >= 0
    assert len(metric["stage_spans"]) == 1
    span = metric["stage_spans"][0]
    assert span["stage"] == "retrieve"
    assert span["status"] == "ok"
    assert span["start_ms"] >= 0
    assert span["duration_ms"] == metric["retrieval_ms"]
    for field in (
        "rewrite_ms",
        "rerank_ms",
        "llm_ms",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "citation_ok",
        "rerank_degraded",
        "error_kind",
    ):
        assert metric.get(field) is None
    assert_request_state(request, response, metric)
    assert len(store.calls) == 1
    assert store.calls[0]["stage"] == "retrieve"
    assert store.calls[0]["hits"] == 1


def test_search_trace_ms_and_metric_total_ms_use_distinct_origins(monkeypatch) -> None:
    ticks = iter([1.0, 6.0, 9.0])
    monkeypatch.setattr(
        search_module,
        "time",
        SimpleNamespace(perf_counter=lambda: next(ticks)),
    )
    calls = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch, Retriever(result=[HIT]))

    invoke_search(make_request(), Response(), store)

    assert store.calls[0]["ms"] == 3000.0
    assert calls[0]["total_ms"] == 8000.0


def test_search_error_records_once_and_reraises_original(monkeypatch) -> None:
    calls = capture_metrics(monkeypatch)
    store = install_dependencies(
        monkeypatch, Retriever(error=LookupError("retrieve exploded"))
    )
    request = make_request()
    response = Response()

    with pytest.raises(LookupError, match="retrieve exploded"):
        invoke_search(request, response, store)

    assert len(calls) == 1
    metric = calls[0]
    assert metric["operation"] == "search"
    assert metric["outcome"] == "error"
    assert metric["hits"] == 0
    assert metric["error_kind"] == "LookupError"
    assert metric["total_ms"] >= metric["retrieval_ms"] >= 0
    assert len(metric["stage_spans"]) == 1
    span = metric["stage_spans"][0]
    assert span["stage"] == "retrieve"
    assert span["status"] == "error"
    assert span["duration_ms"] == metric["retrieval_ms"]
    for field in (
        "rewrite_ms",
        "rerank_ms",
        "llm_ms",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "citation_ok",
        "rerank_degraded",
    ):
        assert metric.get(field) is None
    assert_request_state(request, response, metric)
    assert len(store.calls) == 1
    assert store.calls[0]["stage"] == "error"


def test_real_best_effort_writer_failure_preserves_search_response(
    monkeypatch, caplog
) -> None:
    store = install_dependencies(monkeypatch, Retriever(result=[HIT]))
    writes: list[dict] = []

    def fail_write(engine, **fields) -> None:
        writes.append(fields)
        raise RuntimeError("metric database unavailable")

    monkeypatch.setattr(ops_observability, "record_rag_metric", fail_write)
    monkeypatch.setattr(
        search_module,
        "try_record_rag_metric",
        ops_observability.try_record_rag_metric,
        raising=False,
    )
    request = make_request()
    response = Response()

    with caplog.at_level(logging.ERROR, logger="src.ops_observability"):
        result = invoke_search(request, response, store)

    assert len(writes) == 1
    assert isinstance(result, SearchResponse)
    assert [hit.chunk_id for hit in result.hits] == [HIT.chunk_id]
    assert result.hits[0].text == HIT.text
    assert "failed to record RAG metric" in caplog.text
