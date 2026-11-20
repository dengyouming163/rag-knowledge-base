"""chat usage/耗时的包装器采集：不改 pipeline，只做旁路记录。"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_timed_retriever_records_last_ms() -> None:
    inner = SimpleNamespace(retrieve=lambda *args, **kwargs: ["hit"])
    wrapper = TimedRetriever(inner)
    assert wrapper.retrieve("q") == ["hit"]
    assert wrapper.last_ms >= 0


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
