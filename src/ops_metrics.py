"""调测驾驶舱的纯聚合函数；数据加载由路由负责。

这里只做"把已有记录换算成指标"，不访问数据库、不发网络请求，
便于用极小输入覆盖口径（分母、空值、分位数）。
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


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
    now = datetime.now(timezone.utc)
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


def summarize_ai(traces: list[dict]) -> dict:
    retrieves = [trace for trace in traces if trace.get("stage") == "retrieve"]
    answers = [trace for trace in traces if trace.get("stage") == "answer"]
    citations = [
        trace["citation_ok"] for trace in answers
        if trace.get("citation_ok") is not None
    ]
    reranks = [
        bool(trace.get("rerank_degraded")) for trace in answers
        if trace.get("rerank_degraded") is not None
    ]
    prompt = sum(int(trace.get("prompt_tokens") or 0) for trace in answers)
    completion = sum(int(trace.get("completion_tokens") or 0) for trace in answers)
    llm_latencies = [
        float(trace["llm_ms"]) for trace in answers if trace.get("llm_ms") is not None
    ]
    input_price = os.environ.get("LLM_INPUT_PRICE_PER_1K")
    output_price = os.environ.get("LLM_OUTPUT_PRICE_PER_1K")
    estimated_cost = None
    if input_price and output_price:
        estimated_cost = round(
            prompt / 1000 * float(input_price)
            + completion / 1000 * float(output_price),
            6,
        )
    return {
        "requests": len(answers),
        "refusal_rate": _ratio(
            sum(trace.get("refused") is True for trace in answers), len(answers)
        ),
        "zero_result_rate": _ratio(
            sum(int(trace.get("hits") or 0) == 0 for trace in retrieves),
            len(retrieves),
        ),
        "citation_ok_rate": _ratio(sum(bool(item) for item in citations), len(citations)),
        "rerank_degraded_rate": _ratio(sum(reranks), len(reranks)),
        "tokens": {
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
        },
        "llm_latency_ms": {
            "p50": percentile(llm_latencies, 0.50),
            "p95": percentile(llm_latencies, 0.95),
        },
        "model": next(
            (trace.get("model") for trace in reversed(answers) if trace.get("model")),
            None,
        ),
        "estimated_cost": estimated_cost,
    }


def build_ops_summary(
    *,
    health: dict,
    jobs: list[dict],
    traces: list[dict],
    runs: list[dict],
    workers: list[dict],
) -> dict:
    return {
        "health": health,
        "queue": summarize_queue(jobs),
        "ai": summarize_ai(traces),
        "latest_runs": runs[:5],
        "latest_traces": traces[-10:],
        # latest_workers 与 workers 同值：前者与 latest_runs 命名一致，后者保持计划原始契约
        "workers": workers,
        "latest_workers": workers,
    }
