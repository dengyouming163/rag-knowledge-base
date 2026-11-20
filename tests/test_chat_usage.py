"""chat usage/耗时的包装器采集：不改 pipeline，只做旁路记录。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import observability
from app.observability import (
    TimedRetriever,
    UsageRecordingChatClient,
    usage_from_chat_result,
)


def test_usage_recorder_keeps_last_chat_result() -> None:
    result = SimpleNamespace(
        prompt_tokens=12, completion_tokens=7, model="m", latency_ms=100
    )
    inner = SimpleNamespace(chat=lambda *args, **kwargs: result)
    wrapper = UsageRecordingChatClient(inner)
    assert wrapper.chat("q") is result
    assert wrapper.last is result


def test_usage_recorder_records_every_call_with_origin(monkeypatch) -> None:
    ticks = iter([2.0, 2.04, 3.0, 3.125])
    monkeypatch.setattr(
        observability, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    first = SimpleNamespace(text="rewrite")
    second = SimpleNamespace(text="answer")
    results = iter([first, second])
    inner = SimpleNamespace(chat=lambda *args, **kwargs: next(results))
    wrapper = UsageRecordingChatClient(inner, origin=1.5)

    assert wrapper.chat("rewrite", max_tokens=200) is first
    assert wrapper.chat("answer", temperature=0.0) is second
    assert wrapper.last is second
    assert len(wrapper.calls) == 2
    assert all(
        {"result", "start_ms", "duration_ms"} <= set(call) for call in wrapper.calls
    )
    assert [call.get("stage") for call in wrapper.calls] == ["rewrite", "answer"]
    assert wrapper.calls[0]["result"] is first
    assert wrapper.calls[0]["start_ms"] == 500.0
    assert wrapper.calls[0]["duration_ms"] == pytest.approx(40.0)
    assert wrapper.calls[1]["result"] is second
    assert wrapper.calls[1]["start_ms"] == 1500.0
    assert wrapper.calls[1]["duration_ms"] == pytest.approx(125.0)


def test_timed_retriever_records_last_ms(monkeypatch) -> None:
    ticks = iter([1.0, 1.25])
    monkeypatch.setattr(
        observability, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    inner = SimpleNamespace(retrieve=lambda *args, **kwargs: ["hit"])
    wrapper = TimedRetriever(inner)
    assert wrapper.retrieve("q") == ["hit"]
    assert wrapper.last_ms == 250.0


def test_timed_reranker_returns_original_result_and_records_span(monkeypatch) -> None:
    from app.observability import TimedReranker

    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(
        observability, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    result = SimpleNamespace(hits=["hit"], degraded=False)
    inner = SimpleNamespace(rerank=lambda *args, **kwargs: result)
    wrapper = TimedReranker(inner, origin=9.5)

    assert wrapper.rerank("q", ["hit"], 3) is result
    assert wrapper.last_ms == 125.0
    assert wrapper.last_span == {
        "stage": "rerank",
        "start_ms": 500.0,
        "duration_ms": 125.0,
        "status": "ok",
    }


def test_timed_reranker_reraises_original_error_and_marks_span(monkeypatch) -> None:
    from app.observability import TimedReranker

    ticks = iter([4.0, 4.25])
    monkeypatch.setattr(
        observability, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    error = LookupError("rerank exploded")

    def fail(*args, **kwargs):
        raise error

    wrapper = TimedReranker(SimpleNamespace(rerank=fail), origin=3.0)
    with pytest.raises(LookupError) as caught:
        wrapper.rerank("q", ["hit"], 3)

    assert caught.value is error
    assert wrapper.last_ms == 250.0
    assert wrapper.last_span["status"] == "error"
    assert wrapper.last_span["start_ms"] == 1000.0


def test_usage_from_chat_result_builds_totals() -> None:
    result = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        model="deepseek-chat",
        latency_ms=500,
    )
    usage = usage_from_chat_result(result)
    assert usage["total_tokens"] == 120
    assert usage["model"] == "deepseek-chat"
    assert usage["llm_ms"] == 500


def test_usage_from_missing_result_is_all_zero() -> None:
    usage = usage_from_chat_result(None)
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["model"] == ""
    assert usage["llm_ms"] == 0.0


def test_refused_answer_does_not_record_rewrite_usage(monkeypatch) -> None:
    """多轮改写也会调模型；拒答早退时不能把改写结果当成 answer usage。"""
    from app.routers import chat as chat_module
    from app.schemas import ChatRequest

    class RewriteClient:
        def chat(self, *args, **kwargs):
            return SimpleNamespace(
                text="改写后的问题",
                prompt_tokens=111,
                completion_tokens=5,
                model="rewrite",
                latency_ms=42.0,
            )

    class RefusingPipeline:
        def __init__(self, chat_client) -> None:
            self.chat_client = chat_client

        def answer(self, *args):
            # 有历史时先改写，随后检索不到资料直接拒答，不再调用生成
            self.chat_client.chat([{"role": "user", "content": "q"}], max_tokens=200)
            return {
                "answer": "根据现有资料无法确定",
                "chunks": [],
                "rewritten": "q",
                "rerank_degraded": False,
                "citation_ok": True,
            }

    class Store:
        def __init__(self) -> None:
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)

    monkeypatch.setattr(chat_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: ([], False))
    monkeypatch.setattr(
        chat_module, "build_pipeline", lambda *args: RefusingPipeline(args[4])
    )

    async def exercise():
        store = Store()
        response = await chat_module.chat_stream(
            payload=ChatRequest(kb_id="kb", question="question"),
            tenant_id="t",
            engine=object(),
            embed_client=object(),
            chat_client=RewriteClient(),
            reranker=None,
            user_id="u",
            trace_store=store,
        )
        iterator = response.body_iterator
        hits_frame = await anext(iterator)
        await iterator.aclose()
        return store, hits_frame

    store, frame = asyncio.run(exercise())
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["usage"]["prompt_tokens"] == 0
    assert data["usage"]["model"] == ""
    assert data["timings"]["llm_ms"] == 0

    answer = store.calls[0]
    assert answer["stage"] == "answer"
    assert answer["refused"] is True
    assert "prompt_tokens" not in answer
    assert "model" not in answer
    assert "llm_ms" not in answer
    assert "citation_ok" not in answer
    assert "rerank_degraded" not in answer
