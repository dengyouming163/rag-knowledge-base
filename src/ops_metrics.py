"""调测驾驶舱的纯聚合函数；数据加载由路由负责。

这里只做"把已有记录换算成指标"，不访问数据库、不发网络请求，
便于用极小输入覆盖口径（分母、空值、分位数）。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_queue(jobs: list[dict]) -> dict:
    counts = Counter(job["status"] for job in jobs)
    now = datetime.now(UTC)
    queued_at = [
        job["created_at"] for job in jobs
        if job["status"] == "queued" and job.get("created_at")
    ]
    waits = [
        (job["started_at"] - job["created_at"]).total_seconds()
        for job in jobs
        if job.get("started_at") and job.get("created_at")
    ]
    durations = [
        (job["finished_at"] - job["started_at"]).total_seconds()
        for job in jobs
        if job.get("finished_at") and job.get("started_at")
    ]
    errors = Counter(
        (job.get("error") or "").splitlines()[0][:200]
        for job in jobs if job.get("status") == "failed" and job.get("error")
    )
    return {
        "total": len(jobs),
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "oldest_queued_seconds": (
            max(0.0, (now - min(queued_at)).total_seconds()) if queued_at else None
        ),
        "avg_wait_seconds": round(sum(waits) / len(waits), 2) if waits else None,
        "avg_duration_seconds": round(sum(durations) / len(durations), 2)
        if durations else None,
        "retry_count": sum(max(0, int(job.get("attempts") or 0) - 1) for job in jobs),
        "top_errors": [
            {"error": error, "count": count}
            for error, count in errors.most_common(5)
        ],
    }


def build_ops_summary(
    *,
    health: dict,
    jobs: list[dict],
    observability: dict,
    latest_traces: list[dict],
    runs: list[dict],
    workers: list[dict],
) -> dict:
    rag = observability["rag"]
    tokens = observability["tokens"]
    llm_latency = rag["latency_ms"]["llm"]
    ai = {
        "requests": rag["refusal"]["denominator"],
        "refusal_rate": rag["refusal"]["rate"],
        "zero_result_rate": rag["zero_hit"]["rate"],
        "citation_ok_rate": rag["citation"]["rate"],
        "rerank_degraded_rate": rag["rerank_degraded"]["rate"],
        "tokens": {
            key: tokens[key] for key in ("prompt", "completion", "total")
        },
        "llm_latency_ms": {"p50": llm_latency["p50"], "p95": llm_latency["p95"]},
        "model": rag["model"],
        "estimated_cost": tokens["estimated_cost_usd"],
    }
    return {
        "health": health,
        "queue": summarize_queue(jobs),
        "ai": ai,
        "latest_runs": runs[:5],
        "latest_traces": latest_traces,
        "workers": workers,
        "observability": observability,
    }
