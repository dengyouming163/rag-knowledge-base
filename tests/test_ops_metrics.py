"""驾驶舱纯聚合函数测试：不依赖数据库，只校验指标口径。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.ops_metrics import build_ops_summary, percentile, summarize_queue


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


def test_build_summary_maps_legacy_ai_from_observability() -> None:
    observability = {
        "window": "1h",
        "http": {"requests": 8},
        "rag": {
            "refusal": {"numerator": 1, "denominator": 3, "rate": 0.3333},
            "zero_hit": {"numerator": 1, "denominator": 4, "rate": 0.25},
            "citation": {"numerator": 2, "denominator": 3, "rate": 0.6667},
            "rerank_degraded": {"numerator": 1, "denominator": 2, "rate": 0.5},
            "tokens": {"prompt": 240, "completion": 70, "total": 310},
            "latency_ms": {"llm": {"p50": 55.0, "p95": 77.5, "p99": 79.5, "samples": 2}},
            "model": "m1",
            "cost": {"estimated_cost_usd": 0.00024},
        },
    }
    latest_traces = [{"trace_id": "new"}, {"trace_id": "old"}]
    summary = build_ops_summary(
        health={"api": "up", "db": "up", "parser": "unconfigured", "workers": []},
        jobs=[], observability=observability, latest_traces=latest_traces,
        runs=[], workers=[],
    )

    assert summary["observability"] is observability
    assert summary["latest_traces"] is latest_traces
    assert summary["ai"] == {
        "requests": 3,
        "refusal_rate": 0.3333,
        "zero_result_rate": 0.25,
        "citation_ok_rate": 0.6667,
        "rerank_degraded_rate": 0.5,
        "tokens": {"prompt": 240, "completion": 70, "total": 310},
        "llm_latency_ms": {"p50": 55.0, "p95": 77.5},
        "model": "m1",
        "estimated_cost": 0.00024,
    }
