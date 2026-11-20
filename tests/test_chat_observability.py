"""Chat SSE 终态 RAG 指标与真实阶段跨度。"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import Request

from app.routers import chat as chat_module
from app.schemas import ChatRequest
from src import ops_observability
from src.guard.injection import CANARY
from src.pipeline import GUARD_ANSWER, RagPipeline
from src.store import SearchHit


TENANT = "tenant-from-dependency"
KB = "kb-chat"
ENGINE = object()


class TraceStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


class SequenceChat:
    def __init__(self, results: list) -> None:
        self.results = iter(results)

    def chat(self, *args, **kwargs):
        return next(self.results)


class Retriever:
    def __init__(self, hits: list) -> None:
        self.hits = hits

    def retrieve(self, *args, **kwargs):
        return self.hits


class Reranker:
    def rerank(self, query, hits, top_k):
        return SimpleNamespace(hits=hits[:top_k], degraded=False, reason=None)


class Pipeline:
    def __init__(self, chat_client, reranker, *, behavior: str) -> None:
        self.chat_client = chat_client
        self.reranker = reranker
        self.behavior = behavior
        self.retriever = Retriever([] if behavior == "refused" else ["hit"])

    def answer(self, *args):
        if self.behavior == "error":
            raise RuntimeError("pipeline exploded")
        self.chat_client.chat([{"role": "user", "content": "rewrite"}], max_tokens=200)
        hits = self.retriever.retrieve("rewritten", top_k=5)
        if self.behavior == "refused":
            return {
                "answer": "根据现有资料无法确定",
                "chunks": [],
                "rewritten": "rewritten",
                "rerank_degraded": False,
                "citation_ok": True,
            }
        reranked = self.reranker.rerank("rewritten", hits, 5)
        result = self.chat_client.chat(
            [{"role": "user", "content": "answer"}], temperature=0.0
        )
        return {
            "answer": result.text,
            "chunks": [str(hit) for hit in reranked.hits],
            "rewritten": "rewritten",
            "rerank_degraded": reranked.degraded,
            "citation_ok": True,
        }


def make_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/stream",
            "headers": [],
        }
    )


def chat_results() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            text="rewritten",
            prompt_tokens=111,
            completion_tokens=11,
            model="rewrite-model",
            latency_ms=40.0,
        ),
        SimpleNamespace(
            text="ok",
            prompt_tokens=20,
            completion_tokens=7,
            model="answer-model",
            latency_ms=60.0,
        ),
    ]


def install_dependencies(monkeypatch, behavior: str = "answer") -> TraceStore:
    def check_scope(engine, tenant_id, kb_id) -> None:
        assert engine is ENGINE
        assert tenant_id == TENANT
        assert kb_id == KB

    monkeypatch.setattr(chat_module, "ensure_kb", check_scope)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: (["tag"], False))
    monkeypatch.setattr(
        chat_module,
        "build_pipeline",
        lambda *args: Pipeline(args[4], args[5], behavior=behavior),
    )
    return TraceStore()


def capture_metrics(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def capture(engine, **fields) -> None:
        assert engine is ENGINE
        calls.append(fields)

    monkeypatch.setattr(chat_module, "try_record_rag_metric", capture, raising=False)
    return calls


async def invoke_chat(request: Request, store: TraceStore, chat_client, reranker, history=None):
    return await chat_module.chat_stream(
        payload=ChatRequest(kb_id=KB, question="question", history=history or []),
        tenant_id=TENANT,
        engine=ENGINE,
        embed_client=object(),
        chat_client=chat_client,
        reranker=reranker,
        user_id="user-1",
        trace_store=store,
        request=request,
    )


async def collect(response) -> list[str]:
    return [frame async for frame in response.body_iterator]


def hits_payload(frame: str) -> dict:
    assert frame.startswith("event: hits\ndata: ")
    return json.loads(frame.split("data: ", 1)[1])


def assert_request_state(request: Request, response, metric: dict) -> None:
    assert request.state.kb_id == KB
    assert request.state.trace_id == response.headers["X-Trace-Id"]
    assert metric["trace_id"] == request.state.trace_id


def test_chat_success_records_one_terminal_metric_and_real_stage_spans(monkeypatch) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch)
    request = make_request()

    async def exercise():
        response = await invoke_chat(request, store, SequenceChat(chat_results()), Reranker())
        iterator = response.body_iterator
        frames = [await anext(iterator), await anext(iterator), await anext(iterator)]
        assert metrics == []
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        return response, frames

    response, frames = asyncio.run(exercise())
    assert frames[1] == 'data: {"delta": "ok"}\n\n'
    assert frames[2] == "data: [DONE]\n\n"
    payload = hits_payload(frames[0])
    assert payload["chunks"] == ["hit"]
    assert payload["usage"] == {
        "prompt_tokens": 20,
        "completion_tokens": 7,
        "total_tokens": 27,
        "model": "answer-model",
        "llm_ms": 60.0,
    }
    assert payload["timings"]["llm_ms"] == 60.0

    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["operation"] == "chat"
    assert metric["mode"] == "chat"
    assert metric["outcome"] == "succeeded"
    assert metric["hits"] == 1
    assert metric["model"] == "answer-model"
    assert metric["prompt_tokens"] == 20
    assert metric["completion_tokens"] == 7
    assert metric["citation_ok"] is True
    assert metric["rerank_degraded"] is False
    assert metric.get("error_kind") is None
    spans = metric["stage_spans"]
    assert [span["stage"] for span in spans] == ["rewrite", "retrieve", "rerank", "answer"]
    assert all(span["status"] == "ok" for span in spans)
    assert [span["start_ms"] for span in spans] == sorted(
        span["start_ms"] for span in spans
    )
    assert all(span["start_ms"] >= 0 and span["duration_ms"] >= 0 for span in spans)
    assert metric["rewrite_ms"] == pytest.approx(spans[0]["duration_ms"])
    assert metric["retrieval_ms"] == pytest.approx(spans[1]["duration_ms"])
    assert metric["rerank_ms"] == pytest.approx(spans[2]["duration_ms"])
    assert metric["llm_ms"] == pytest.approx(spans[3]["duration_ms"])
    assert metric["total_ms"] >= max(
        span["start_ms"] + span["duration_ms"] for span in spans
    )
    assert_request_state(request, response, metric)
    assert [call["stage"] for call in store.calls] == ["answer"]
    assert store.calls[0]["prompt_tokens"] == 20
    assert store.calls[0]["model"] == "answer-model"
    assert store.calls[0]["llm_ms"] == 60.0


def test_early_refusal_excludes_answer_usage_and_unexecuted_stages(monkeypatch) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch, behavior="refused")
    request = make_request()

    async def exercise():
        response = await invoke_chat(
            request, store, SequenceChat(chat_results()[:1]), Reranker()
        )
        return response, await collect(response)

    response, frames = asyncio.run(exercise())
    assert frames[-1] == "data: [DONE]\n\n"
    payload = hits_payload(frames[0])
    assert payload["usage"]["prompt_tokens"] == 0
    assert payload["usage"]["model"] == ""
    assert payload["timings"]["llm_ms"] == 0

    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["outcome"] == "refused"
    assert metric["hits"] == 0
    assert metric["rewrite_ms"] is not None
    assert metric["retrieval_ms"] is not None
    for field in (
        "rerank_ms",
        "llm_ms",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "citation_ok",
        "rerank_degraded",
    ):
        assert metric.get(field) is None
    assert [span["stage"] for span in metric["stage_spans"]] == ["rewrite", "retrieve"]
    assert_request_state(request, response, metric)
    assert store.calls[0]["stage"] == "answer"
    assert store.calls[0]["refused"] is True


def test_pipeline_error_finalizes_before_unchanged_error_frame(monkeypatch) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch, behavior="error")
    request = make_request()

    async def exercise():
        response = await invoke_chat(request, store, SequenceChat([]), Reranker())
        iterator = response.body_iterator
        frame = await anext(iterator)
        assert len(metrics) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        return response, frame

    response, frame = asyncio.run(exercise())
    assert frame == 'event: error\ndata: {"message": "pipeline exploded"}\n\n'
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["outcome"] == "error"
    assert metric["error_kind"] == "RuntimeError"
    assert metric["hits"] == 0
    assert metric["stage_spans"] == []
    for field in ("llm_ms", "model", "prompt_tokens", "completion_tokens", "citation_ok"):
        assert metric.get(field) is None
    assert_request_state(request, response, metric)
    assert [call["stage"] for call in store.calls] == ["error"]


def test_closing_after_hits_records_cancelled_once_not_succeeded(monkeypatch) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_dependencies(monkeypatch)
    request = make_request()

    async def exercise():
        response = await invoke_chat(request, store, SequenceChat(chat_results()), Reranker())
        iterator = response.body_iterator
        frame = await anext(iterator)
        assert hits_payload(frame)["chunks"] == ["hit"]
        assert metrics == []
        await iterator.aclose()
        await iterator.aclose()
        return response

    response = asyncio.run(exercise())
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["outcome"] == "cancelled"
    assert metric["hits"] == 1
    assert [span["stage"] for span in metric["stage_spans"]] == [
        "rewrite",
        "retrieve",
        "rerank",
        "answer",
    ]
    assert_request_state(request, response, metric)
    assert [call["stage"] for call in store.calls] == ["answer"]


def test_real_best_effort_writer_failure_preserves_sse_frames(monkeypatch, caplog) -> None:
    store = install_dependencies(monkeypatch)
    writes: list[dict] = []

    def fail_write(engine, **fields) -> None:
        writes.append(fields)
        raise RuntimeError("metric database unavailable")

    monkeypatch.setattr(ops_observability, "record_rag_metric", fail_write)
    monkeypatch.setattr(
        chat_module,
        "try_record_rag_metric",
        ops_observability.try_record_rag_metric,
        raising=False,
    )

    async def exercise():
        response = await invoke_chat(
            make_request(), store, SequenceChat(chat_results()), Reranker()
        )
        return await collect(response)

    with caplog.at_level(logging.ERROR, logger="src.ops_observability"):
        frames = asyncio.run(exercise())

    assert len(writes) == 1
    payload = hits_payload(frames[0])
    assert payload["chunks"] == ["hit"]
    assert payload["rewritten"] == "rewritten"
    assert payload["usage"]["prompt_tokens"] == 20
    assert payload["usage"]["completion_tokens"] == 7
    assert payload["usage"]["model"] == "answer-model"
    assert frames[1] == 'data: {"delta": "ok"}\n\n'
    assert frames[2] == "data: [DONE]\n\n"
    assert "failed to record RAG metric" in caplog.text


HISTORY = [
    {"role": "user", "content": "退款政策是什么？"},
    {"role": "assistant", "content": "签收七天内可无理由退款。"},
]


def make_hit() -> SearchHit:
    return SearchHit(chunk_id="kb#1", doc_id="kb", text="退款政策正文", score=1.0)


class PipelineChat:
    """按真实改写提示词区分 rewrite，其余调用均走 answer 边界。"""

    def __init__(self, answer) -> None:
        self.answer = answer

    def chat(self, messages, **kwargs):
        if "改写" in messages[-1]["content"]:
            return SimpleNamespace(
                text="rewritten",
                prompt_tokens=111,
                completion_tokens=11,
                model="rewrite-model",
                latency_ms=40.0,
            )
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def install_real_pipeline(monkeypatch) -> TraceStore:
    monkeypatch.setattr(chat_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: ([], False))

    def build(*args):
        return RagPipeline(
            chat_client=args[4],
            embed_client=object(),
            store=None,
            bm25_index=None,
            retriever=Retriever([make_hit()]),
            reranker=args[5],
            min_score=0.0,
        )

    monkeypatch.setattr(chat_module, "build_pipeline", build)
    return TraceStore()


@pytest.mark.parametrize("with_history", [False, True], ids=["answer-only", "rewrite-then-answer"])
def test_answer_generation_failure_preserves_answer_error_span(monkeypatch, with_history) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_real_pipeline(monkeypatch)

    async def exercise():
        response = await invoke_chat(
            make_request(),
            store,
            PipelineChat(RuntimeError("answer exploded")),
            Reranker(),
            history=HISTORY if with_history else [],
        )
        iterator = response.body_iterator
        frame = await anext(iterator)
        assert len(metrics) == 1
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        return frame

    frame = asyncio.run(exercise())
    assert frame == 'event: error\ndata: {"message": "answer exploded"}\n\n'
    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["outcome"] == "error"
    assert metric["error_kind"] == "RuntimeError"
    chat_spans = [
        span for span in metric["stage_spans"] if span["stage"] in {"rewrite", "answer"}
    ]
    assert [span["stage"] for span in chat_spans] == (
        ["rewrite", "answer"] if with_history else ["answer"]
    )
    answer_span = chat_spans[-1]
    assert answer_span["status"] == "error"
    assert metric["llm_ms"] == pytest.approx(answer_span["duration_ms"])
    if with_history:
        assert metric["rewrite_ms"] == pytest.approx(chat_spans[0]["duration_ms"])
    else:
        assert metric["rewrite_ms"] is None
    for field in ("model", "prompt_tokens", "completion_tokens", "citation_ok"):
        assert metric.get(field) is None
    assert [call["stage"] for call in store.calls] == ["error"]


def test_guard_refusal_preserves_successful_answer_usage_and_span(monkeypatch) -> None:
    metrics = capture_metrics(monkeypatch)
    store = install_real_pipeline(monkeypatch)
    answer = SimpleNamespace(
        text=f"内部校验串为 {CANARY}",
        prompt_tokens=20,
        completion_tokens=7,
        model="answer-model",
        latency_ms=60.0,
    )

    async def exercise():
        response = await invoke_chat(
            make_request(), store, PipelineChat(answer), Reranker(), history=HISTORY
        )
        return await collect(response)

    frames = asyncio.run(exercise())
    payload = hits_payload(frames[0])
    assert payload["chunks"] == []
    assert payload["usage"]["prompt_tokens"] == 0
    assert payload["usage"]["model"] == ""
    deltas = [
        json.loads(frame[len("data: ") :])["delta"]
        for frame in frames
        if frame.startswith("data: {")
    ]
    assert "".join(deltas) == GUARD_ANSWER
    assert frames[-1] == "data: [DONE]\n\n"

    assert len(metrics) == 1
    metric = metrics[0]
    assert metric["outcome"] == "refused"
    assert metric["hits"] == 0
    assert metric["model"] == "answer-model"
    assert metric["prompt_tokens"] == 20
    assert metric["completion_tokens"] == 7
    assert metric["citation_ok"] is True
    chat_spans = [
        span for span in metric["stage_spans"] if span["stage"] in {"rewrite", "answer"}
    ]
    assert [span["stage"] for span in chat_spans] == ["rewrite", "answer"]
    assert chat_spans[-1]["status"] == "ok"
    assert metric["llm_ms"] == pytest.approx(chat_spans[-1]["duration_ms"])
    assert metric["rewrite_ms"] == pytest.approx(chat_spans[0]["duration_ms"])
