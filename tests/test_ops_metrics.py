"""驾驶舱纯聚合函数测试：不依赖数据库，只校验指标口径。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.ops_metrics import build_ops_summary, percentile, summarize_ai, summarize_queue


def test_percentile_interpolates() -> None:
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert percentile([], 0.95) is None


def test_queue_counts_wait_and_duration() -> None:
    now = datetime.now(timezone.utc)
    jobs = [
        {"status": "queued", "created_at": now - timedelta(seconds=10), "started_at": None,
         "finished_at": None},
        {"status": "succeeded", "created_at": now - timedelta(seconds=30),
         "started_at": now - timedelta(seconds=20), "finished_at": now - timedelta(seconds=5)},
        {"status": "failed", "created_at": now - timedelta(seconds=25),
         "started_at": now - timedelta(seconds=20), "finished_at": now - timedelta(seconds=2)},
    ]
    summary = summarize_queue(jobs)
    assert summary["queued"] == 1
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["oldest_queued_seconds"] >= 9
    assert summary["avg_duration_seconds"] == 16.5


def test_ai_metrics_use_real_denominators() -> None:
    traces = [
        {"stage": "retrieve", "hits": 0, "refused": False, "mode": "hybrid"},
        {"stage": "retrieve", "hits": 3, "refused": False, "mode": "hybrid"},
        {"stage": "answer", "hits": 3, "refused": False, "mode": "chat",
         "prompt_tokens": 100, "completion_tokens": 20, "llm_ms": 500,
         "citation_ok": True, "rerank_degraded": False},
        {"stage": "answer", "hits": 0, "refused": True, "mode": "chat",
         "prompt_tokens": 50, "completion_tokens": 0, "llm_ms": 300,
         "citation_ok": None, "rerank_degraded": True},
    ]
    summary = summarize_ai(traces)
    assert summary["requests"] == 2
    assert summary["refusal_rate"] == 0.5
    assert summary["zero_result_rate"] == 0.5
    assert summary["tokens"]["total"] == 170
    assert summary["llm_latency_ms"]["p50"] == 400
    assert summary["citation_ok_rate"] == 1.0
    assert summary["rerank_degraded_rate"] == 0.5


def test_build_summary_keeps_empty_values_nullable() -> None:
    summary = build_ops_summary(
        health={"api": "up", "db": "up", "parser": "unconfigured", "workers": []},
        jobs=[], traces=[], runs=[], workers=[],
    )
    assert summary["queue"]["total"] == 0
    assert summary["ai"]["requests"] == 0
    assert summary["ai"]["refusal_rate"] is None
