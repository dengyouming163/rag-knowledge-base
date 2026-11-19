"""运营报表的纯聚合函数；数据加载由调用方负责。"""

from __future__ import annotations

from collections import Counter

_BUSINESS_STAGES = {"retrieve", "answer"}
_JOB_STATUSES = ("queued", "running", "succeeded", "failed")


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().split()).casefold()


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def build_stats(
    kb_id: str, traces: list[dict], jobs: list[dict], top_n: int = 10
) -> dict:
    business = [trace for trace in traces if trace.get("stage") in _BUSINESS_STAGES]
    answers = [trace for trace in business if trace["stage"] == "answer"]
    covered = sum(trace["hits"] > 0 for trace in business)

    zero_results: Counter[str] = Counter()
    gaps: dict[str, dict] = {}
    for trace in business:
        query = _normalize_query(trace["query"])
        zero = trace["hits"] == 0
        refused = trace["refused"] is True
        if zero:
            zero_results[query] += 1
        if zero or refused:
            item = gaps.setdefault(
                query, {"query": query, "count": 0, "zero_results": 0, "refusals": 0}
            )
            item["count"] += 1
            item["zero_results"] += int(zero)
            item["refusals"] += int(refused)

    zero_result_top = [
        {"query": query, "count": count}
        for query, count in sorted(zero_results.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    ]
    gap_top = sorted(gaps.values(), key=lambda item: (-item["count"], item["query"]))[:top_n]

    ingest = {status: sum(job["status"] == status for job in jobs) for status in _JOB_STATUSES}
    terminal = ingest["succeeded"] + ingest["failed"]
    return {
        "kb_id": kb_id,
        "total_requests": len(business),
        "coverage": _ratio(covered, len(business)),
        "refusal_rate": _ratio(sum(trace["refused"] is True for trace in answers), len(answers)),
        "zero_result_top": zero_result_top,
        "gap_top": gap_top,
        "ingest": {
            "total": len(jobs),
            **ingest,
            "success_rate": _ratio(ingest["succeeded"], terminal),
        },
    }
