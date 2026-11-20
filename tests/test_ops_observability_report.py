"""时间窗原始指标聚合与摘要 Trace 查询测试。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, text

from src.ops_observability import record_http_metric, record_rag_metric
from src.ops_observability_report import (
    list_rag_metric_rows,
    resolve_window,
    summarize_observability,
)
from src.ops_pricing import parse_pricing

NOW = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(hours=1)


def _http(engine, tenant: str, kb: str, trace_id: str, occurred_at: datetime,
          status_code: int, duration_ms: float) -> None:
    record_http_metric(
        engine, tenant_id=tenant, kb_id=kb, route="/v1/test", method="GET",
        status_code=status_code, duration_ms=duration_ms, trace_id=trace_id,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_http_metrics SET occurred_at=:occurred_at "
                "WHERE tenant_id=:tenant AND trace_id=:trace_id"
            ),
            {"occurred_at": occurred_at, "tenant": tenant, "trace_id": trace_id},
        )


def _rag(engine, tenant: str, kb: str, trace_id: str, occurred_at: datetime,
         **overrides) -> None:
    fields = {
        "operation": "chat",
        "mode": "hybrid",
        "outcome": "succeeded",
        "hits": 1,
        "total_ms": 100.0,
        "stage_spans": [],
    }
    fields.update(overrides)
    record_rag_metric(
        engine, trace_id=trace_id, tenant_id=tenant, kb_id=kb, **fields
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_rag_metrics SET occurred_at=:occurred_at "
                "WHERE tenant_id=:tenant AND trace_id=:trace_id"
            ),
            {"occurred_at": occurred_at, "tenant": tenant, "trace_id": trace_id},
        )


@pytest.fixture()
def populated_metrics(metric_scope):
    engine, tenant, kb = metric_scope
    other_tenant = f"{tenant}_other"
    other_kb = f"{kb}_other"

    for index, (occurred_at, status, duration) in enumerate([
        (START, 200, 100),
        (START + timedelta(minutes=10), 404, 200),
        (START + timedelta(minutes=20), 500, 300),
        (NOW - timedelta(microseconds=1), 201, 400),
        (NOW, 500, 10_000),
        (START - timedelta(microseconds=1), 500, 20_000),
    ]):
        _http(engine, tenant, kb, f"http-{index}", occurred_at, status, duration)
    _http(engine, other_tenant, kb, "http-other-tenant", START, 500, 30_000)
    _http(engine, tenant, other_kb, "http-other-kb", START, 500, 40_000)

    rows = [
        ("rag-a", START, {
            "hits": 0, "total_ms": 100, "retrieval_ms": 10, "rerank_ms": 20,
            "llm_ms": 30, "model": "m1", "prompt_tokens": 100,
            "completion_tokens": 50, "citation_ok": True, "rerank_degraded": False,
        }),
        ("rag-b", START + timedelta(minutes=5), {
            "outcome": "refused", "hits": 0, "total_ms": 200, "retrieval_ms": 20,
            "model": "m1", "prompt_tokens": 40, "completion_tokens": 0,
        }),
        ("rag-c", START + timedelta(minutes=10), {
            "operation": "search", "hits": 3, "total_ms": 300, "retrieval_ms": 30,
            "rerank_ms": 40, "rerank_degraded": True,
        }),
        ("rag-d", NOW - timedelta(microseconds=1), {
            "hits": 2, "total_ms": 400, "retrieval_ms": 40, "rerank_ms": 60,
            "llm_ms": 80, "model": "unknown", "prompt_tokens": 100,
            "completion_tokens": 20, "citation_ok": False, "rerank_degraded": True,
        }),
        ("rag-error", START + timedelta(minutes=20), {
            "outcome": "error", "hits": 0, "total_ms": 500, "error_kind": "llm",
        }),
        ("rag-cancelled", START + timedelta(minutes=30), {
            "operation": "search", "outcome": "cancelled", "hits": 0, "total_ms": 600,
        }),
        ("rag-end", NOW, {"outcome": "refused", "hits": 0, "total_ms": 700}),
        ("rag-before", START - timedelta(microseconds=1), {
            "outcome": "refused", "hits": 0, "total_ms": 800,
        }),
    ]
    for trace_id, occurred_at, overrides in rows:
        _rag(engine, tenant, kb, trace_id, occurred_at, **overrides)
    _rag(engine, other_tenant, kb, "rag-other-tenant", START, hits=0)
    _rag(engine, tenant, other_kb, "rag-other-kb", START, hits=0)

    try:
        yield engine, tenant, kb
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ops_http_metrics WHERE tenant_id=:tenant"),
                {"tenant": other_tenant},
            )
            connection.execute(
                text("DELETE FROM ops_rag_metrics WHERE tenant_id=:tenant"),
                {"tenant": other_tenant},
            )


@pytest.mark.parametrize(
    "name, delta, bucket_seconds",
    [
        ("1h", timedelta(hours=1), 60),
        ("24h", timedelta(hours=24), 300),
        ("7d", timedelta(days=7), 3600),
        ("30d", timedelta(days=30), 21600),
    ],
)
def test_resolve_window_uses_exact_utc_half_open_bounds(name, delta, bucket_seconds):
    start, end, resolved_bucket = resolve_window(name, now=NOW)
    assert (start, end, resolved_bucket) == (NOW - delta, NOW, bucket_seconds)


@pytest.mark.parametrize("name", ["", "1m", "24H", None])
def test_resolve_window_rejects_invalid_names(name):
    with pytest.raises(ValueError, match="invalid window"):
        resolve_window(name, now=NOW)


def test_resolve_window_rejects_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_window("1h", now=datetime(2026, 2, 1, 12, 0))


def test_summarize_observability_uses_raw_scoped_rows_and_two_queries(
    populated_metrics,
) -> None:
    engine, tenant, kb = populated_metrics
    pricing = parse_pricing(
        '{"m1": {"prompt_per_1m": 1, "completion_per_1m": 2}}'
    )
    statements = []

    def collect_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", collect_statement)
    try:
        result = summarize_observability(
            engine, tenant, kb, "1h", pricing, now=NOW
        )
    finally:
        event.remove(engine, "before_cursor_execute", collect_statement)

    assert len(statements) == 2
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH"))
        for statement in statements
    )
    assert result["window"] == "1h"
    assert result["start"] == START.isoformat()
    assert result["end"] == NOW.isoformat()
    assert result["bucket_seconds"] == 60

    http = result["http"]
    assert http["requests"] == 4
    assert http["avg_qpm"] == 0.0667
    assert http["4xx_rate"] == 0.25
    assert http["5xx_rate"] == 0.25
    assert http["duration_ms"] == {
        "p50": 250.0, "p95": 385.0, "p99": 397.0, "samples": 4
    }

    rag = result["rag"]
    assert rag["operations"] == 6
    assert rag["zero_hit"] == {"numerator": 2, "denominator": 4, "rate": 0.5}
    assert rag["refusal"] == {"numerator": 1, "denominator": 3, "rate": 0.3333}
    assert rag["rerank_degraded"] == {
        "numerator": 2, "denominator": 3, "rate": 0.6667
    }
    assert rag["citation"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert rag["latency_ms"] == {
        "e2e": {"p50": 350.0, "p95": 575.0, "p99": 595.0, "samples": 6},
        "retrieve": {
            "p50": 25.0, "p95": 38.5, "p99": pytest.approx(39.7), "samples": 4
        },
        "rerank": {"p50": 40.0, "p95": 58.0, "p99": 59.6, "samples": 3},
        "llm": {"p50": 55.0, "p95": 77.5, "p99": 79.5, "samples": 2},
    }
    assert rag["tokens"] == {"prompt": 240, "completion": 70, "total": 310}
    assert rag["model"] == "unknown"
    assert rag["cost"] == {
        "pricing_configured": True,
        "pricing_complete": False,
        "estimated_cost_usd": 0.00024,
        "priced_tokens": 190,
        "unpriced_tokens": 120,
    }


def test_empty_window_uses_null_rates_and_independent_zero_samples(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    result = summarize_observability(engine, tenant, kb, "24h", {}, now=NOW)

    assert result["http"]["requests"] == 0
    assert result["http"]["avg_qpm"] == 0.0
    assert result["http"]["4xx_rate"] is None
    assert result["http"]["5xx_rate"] is None
    assert result["http"]["duration_ms"] == {
        "p50": None, "p95": None, "p99": None, "samples": 0
    }
    rag = result["rag"]
    assert rag["operations"] == 0
    for key in ("zero_hit", "refusal", "rerank_degraded", "citation"):
        assert rag[key] == {"numerator": 0, "denominator": 0, "rate": None}
    for quantiles in rag["latency_ms"].values():
        assert quantiles == {"p50": None, "p95": None, "p99": None, "samples": 0}
    assert rag["tokens"] == {"prompt": 0, "completion": 0, "total": 0}
    assert rag["cost"] == {
        "pricing_configured": False,
        "pricing_complete": False,
        "estimated_cost_usd": None,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
    }


def test_estimation_value_error_degrades_without_hiding_metrics(
    metric_scope, caplog
) -> None:
    engine, tenant, kb = metric_scope
    _rag(
        engine, tenant, kb, "rag-overflow", NOW - timedelta(minutes=1),
        model="m1", prompt_tokens=1_000_000, completion_tokens=0,
    )
    pricing = {
        "m1": {"prompt_per_1m": Decimal("1e309"), "completion_per_1m": Decimal(0)}
    }

    with caplog.at_level(logging.ERROR, logger="src.ops_observability_report"):
        result = summarize_observability(engine, tenant, kb, "1h", pricing, now=NOW)

    assert result["rag"]["operations"] == 1
    assert result["rag"]["cost"] == {
        "pricing_configured": False,
        "pricing_complete": False,
        "estimated_cost_usd": None,
        "priced_tokens": 0,
        "unpriced_tokens": 1_000_000,
    }
    assert "cost estimation failed" in caplog.text


def test_list_rag_metric_rows_is_scoped_stable_and_limited(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    same_time = NOW - timedelta(minutes=1)
    for trace_id in ("row-a", "row-b", "row-c"):
        _rag(
            engine, tenant, kb, trace_id, same_time, mode="vector",
            citation_ok=True, rerank_degraded=False, retrieval_ms=12.5,
            prompt_tokens=3, completion_tokens=2,
        )
    _rag(engine, tenant, f"{kb}_other", "other-kb", NOW)
    other_tenant = f"{tenant}_other"
    _rag(engine, other_tenant, kb, "other-tenant", NOW)
    try:
        rows = list_rag_metric_rows(engine, tenant, kb, limit=2)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM ops_rag_metrics WHERE tenant_id=:tenant"),
                {"tenant": other_tenant},
            )

    assert len(rows) == 2
    assert {row["trace_id"] for row in rows} == {"row-b", "row-c"}
    assert rows[0]["id"] > rows[1]["id"]
    assert rows[0]["occurred_at"] == same_time
    assert set(rows[0]) == {
        "id", "trace_id", "occurred_at", "operation", "mode", "outcome", "hits",
        "citation_ok", "rerank_degraded", "total_ms", "rewrite_ms", "retrieval_ms",
        "rerank_ms", "llm_ms", "model", "prompt_tokens", "completion_tokens",
        "error_kind", "stage_spans",
    }
