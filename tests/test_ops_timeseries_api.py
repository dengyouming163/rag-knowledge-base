"""固定桶运维趋势接口：桶边界、原始行分位数、成本与租户隔离。"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app import deps
from app.main import create_app
from src import ops_observability_report as report
from src.ops_observability import record_http_metric, record_rag_metric

NOW = datetime(2026, 2, 1, 12, 0, 30, tzinfo=UTC)
START = NOW - timedelta(hours=1)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


@pytest.fixture()
def client(metric_scope, monkeypatch):
    engine, _, _ = metric_scope
    application = create_app()
    application.dependency_overrides[deps.get_engine] = lambda: engine
    monkeypatch.delenv("OPS_MODEL_PRICING_JSON", raising=False)
    monkeypatch.setattr(report, "datetime", _FixedDateTime)

    def unexpected_trace_read(*args, **kwargs):
        raise AssertionError("timeseries must not read JSONL traces")

    monkeypatch.setattr("src.trace.TraceStore.list_records", unexpected_trace_read)
    monkeypatch.setattr("src.trace.TraceStore.list_recent", unexpected_trace_read)
    with TestClient(application) as test_client:
        yield test_client
    application.dependency_overrides.clear()


def _get(client: TestClient, tenant: str, kb: str, window: str | None = None):
    params = {"kb_id": kb}
    if window is not None:
        params["window"] = window
    return client.get(
        "/v1/ops/metrics/timeseries",
        params=params,
        headers={"X-Tenant-Id": tenant},
    )


def _http(engine, tenant: str, kb: str, trace_id: str, occurred_at: datetime,
          status_code: int, duration_ms: float = 100) -> None:
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


def _empty_point(ts: str) -> dict:
    return {
        "ts": ts,
        "requests": 0,
        "client_errors": 0,
        "server_errors": 0,
        "e2e_p95_ms": None,
        "retrieval_p95_ms": None,
        "llm_p95_ms": None,
        "total_tokens": 0,
        "estimated_cost_usd": None,
        "pricing_configured": False,
        "pricing_complete": False,
        "priced_tokens": 0,
        "unpriced_tokens": 0,
    }


@pytest.mark.parametrize(
    "window, delta, bucket_seconds, bucket_count",
    [
        ("1h", timedelta(hours=1), 60, 60),
        ("24h", timedelta(hours=24), 300, 288),
        ("7d", timedelta(days=7), 3600, 168),
        ("30d", timedelta(days=30), 21600, 120),
    ],
)
def test_timeseries_uses_fixed_aligned_buckets_and_empty_values(
    client: TestClient, metric_scope, window, delta, bucket_seconds, bucket_count
) -> None:
    _, tenant, kb = metric_scope

    # 24h 同时验证省略 window 时的默认值。
    response = _get(client, tenant, kb, None if window == "24h" else window)

    assert response.status_code == 200
    body = response.json()
    start = NOW - delta
    first_epoch = math.floor(start.timestamp() / bucket_seconds) * bucket_seconds
    expected_ts = [
        datetime.fromtimestamp(first_epoch + index * bucket_seconds, UTC)
        .isoformat()
        for index in range(bucket_count)
    ]
    assert set(body) == {"window", "bucket_seconds", "start_at", "end_at", "series"}
    assert body["window"] == window
    assert body["bucket_seconds"] == bucket_seconds
    assert body["start_at"] == start.isoformat()
    assert body["end_at"] == NOW.isoformat()
    assert len(body["series"]) == bucket_count
    assert [point["ts"] for point in body["series"]] == expected_ts
    assert body["series"] == [_empty_point(ts) for ts in expected_ts]


def test_timeseries_half_open_window_and_partial_buckets_count_rows_once(
    client: TestClient, metric_scope
) -> None:
    engine, tenant, kb = metric_scope
    first_epoch = math.floor(START.timestamp() / 60) * 60
    boundary = datetime.fromtimestamp(first_epoch + 60, UTC)
    rows = [
        ("before", START - timedelta(microseconds=1), 500, 10_000),
        ("start", START, 200, 100),
        ("boundary", boundary, 404, 200),
        ("last", NOW - timedelta(microseconds=1), 500, 300),
        ("end", NOW, 500, 20_000),
    ]
    for trace_id, occurred_at, status, duration in rows:
        _http(engine, tenant, kb, f"http-{trace_id}", occurred_at, status, duration)
        _rag(
            engine, tenant, kb, f"rag-{trace_id}", occurred_at,
            total_ms=duration, retrieval_ms=duration / 10, llm_ms=duration / 20,
            model="m1", prompt_tokens=100 if trace_id in {"before", "end"} else 10,
            completion_tokens=5,
        )

    response = _get(client, tenant, kb, "1h")

    assert response.status_code == 200
    series = response.json()["series"]
    assert sum(point["requests"] for point in series) == 3
    assert sum(point["client_errors"] for point in series) == 1
    assert sum(point["server_errors"] for point in series) == 1
    assert series[0]["requests"] == 1
    assert series[0]["e2e_p95_ms"] == 100
    assert series[0]["retrieval_p95_ms"] == 10
    assert series[0]["llm_p95_ms"] == 5
    assert series[1]["requests"] == 1
    assert series[1]["client_errors"] == 1
    assert series[1]["e2e_p95_ms"] == 200
    assert series[-1]["requests"] == 1
    assert series[-1]["server_errors"] == 1
    assert series[-1]["e2e_p95_ms"] == 300
    assert sum(point["total_tokens"] for point in series) == 45


def test_timeseries_uses_raw_bucket_quantiles_model_costs_and_two_queries(
    client: TestClient, metric_scope, monkeypatch
) -> None:
    engine, tenant, kb = metric_scope
    monkeypatch.setenv(
        "OPS_MODEL_PRICING_JSON",
        '{"m1": {"prompt_per_1m": 1, "completion_per_1m": 2}}',
    )
    rows = [
        ("a", START + timedelta(seconds=1), 100, 10, 30, "m1", 100, 50),
        ("b", START + timedelta(seconds=2), 200, 20, 40, "m1", 40, 10),
        ("c", START + timedelta(seconds=3), 300, 30, 60, "unknown", 100, 20),
        ("d", START + timedelta(seconds=61), 1000, 100, 300, "m1", 1000, 500),
    ]
    for trace_id, occurred_at, total, retrieval, llm, model, prompt, completion in rows:
        _rag(
            engine, tenant, kb, trace_id, occurred_at,
            total_ms=total, retrieval_ms=retrieval, llm_ms=llm, model=model,
            prompt_tokens=prompt, completion_tokens=completion,
        )
    statements = []

    def collect_statement(conn, cursor, statement, parameters, context, executemany):
        if "ops_http_metrics" in statement or "ops_rag_metrics" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", collect_statement)
    try:
        response = _get(client, tenant, kb, "1h")
    finally:
        event.remove(engine, "before_cursor_execute", collect_statement)

    assert response.status_code == 200
    assert len(statements) == 2
    assert sum("ops_http_metrics" in statement for statement, _ in statements) == 1
    assert sum("ops_rag_metrics" in statement for statement, _ in statements) == 1
    for statement, parameters in statements:
        assert "bucket_seconds" in statement
        assert parameters["bucket_seconds"] == 60
        assert parameters["start"] == START
        assert parameters["end"] == NOW

    series = response.json()["series"]
    assert series[0]["e2e_p95_ms"] == 290
    assert series[0]["retrieval_p95_ms"] == 29
    assert series[0]["llm_p95_ms"] == 58
    assert series[0]["total_tokens"] == 320
    assert series[0]["estimated_cost_usd"] == 0.00026
    assert series[0]["pricing_configured"] is True
    assert series[0]["pricing_complete"] is False
    assert series[0]["priced_tokens"] == 200
    assert series[0]["unpriced_tokens"] == 120
    assert series[1]["e2e_p95_ms"] == 1000
    assert series[1]["retrieval_p95_ms"] == 100
    assert series[1]["llm_p95_ms"] == 300
    assert series[1]["total_tokens"] == 1500
    assert series[1]["estimated_cost_usd"] == 0.002
    assert series[1]["pricing_complete"] is True
    assert series[1]["priced_tokens"] == 1500
    assert series[1]["unpriced_tokens"] == 0

    summary = client.get(
        "/v1/ops/summary", params={"kb_id": kb, "window": "1h"},
        headers={"X-Tenant-Id": tenant},
    )
    assert summary.status_code == 200
    observability = summary.json()["observability"]
    assert observability["rag"]["latency_ms"]["e2e"]["p95"] == pytest.approx(895)
    assert observability["rag"]["latency_ms"]["e2e"]["p95"] != (
        series[0]["e2e_p95_ms"] + series[1]["e2e_p95_ms"]
    ) / 2
    assert observability["tokens"] == {
        "prompt": 1240,
        "completion": 580,
        "total": 1820,
        "pricing_configured": True,
        "pricing_complete": False,
        "estimated_cost_usd": 0.00226,
        "priced_tokens": 1700,
        "unpriced_tokens": 120,
    }


def test_timeseries_rejects_cross_tenant_kb(client: TestClient, metric_scope) -> None:
    engine, tenant, kb = metric_scope
    other_tenant = f"{tenant}_other"
    other_kb = f"{kb}_other"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'other')"),
            {"t": other_tenant},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'other', '', 'active')"
            ),
            {"kb": other_kb, "t": other_tenant},
        )
    try:
        assert _get(client, tenant, kb, "1h").status_code == 200
        assert _get(client, tenant, other_kb, "1h").status_code == 404
        assert _get(client, other_tenant, kb, "1h").status_code == 404
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": other_kb}
            )
            connection.execute(
                text("DELETE FROM tenants WHERE id = :t"), {"t": other_tenant}
            )


def test_timeseries_requires_kb_and_rejects_invalid_window(
    client: TestClient, metric_scope, monkeypatch
) -> None:
    _, tenant, kb = metric_scope
    assert _get(client, tenant, kb, "1h").status_code == 200

    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", "{")
    invalid = _get(client, tenant, kb, "1m")
    missing = client.get(
        "/v1/ops/metrics/timeseries", headers={"X-Tenant-Id": tenant}
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "invalid window"
    assert missing.status_code == 422


def test_timeseries_invalid_pricing_degrades_cost_without_failing(
    client: TestClient, metric_scope, monkeypatch, caplog
) -> None:
    engine, tenant, kb = metric_scope
    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", "{")
    _rag(engine, tenant, kb, "invalid-pricing", START, model="m1", prompt_tokens=100)

    with caplog.at_level(logging.ERROR, logger="app.routers.ops"):
        response = _get(client, tenant, kb, "1h")

    assert response.status_code == 200
    point = response.json()["series"][0]
    assert point["total_tokens"] == 100
    assert point["estimated_cost_usd"] is None
    assert point["pricing_configured"] is False
    assert point["pricing_complete"] is False
    assert point["priced_tokens"] == 0
    assert point["unpriced_tokens"] == 100
    assert "invalid model pricing configuration" in caplog.text


def test_timeseries_estimation_value_error_degrades_without_failing(
    client: TestClient, metric_scope, monkeypatch, caplog
) -> None:
    engine, tenant, kb = metric_scope
    monkeypatch.setenv(
        "OPS_MODEL_PRICING_JSON",
        '{"m1": {"prompt_per_1m": 1, "completion_per_1m": 2}}',
    )
    _rag(engine, tenant, kb, "estimation-error", START, model="m1", prompt_tokens=100)

    def fail_estimation(*args, **kwargs):
        raise ValueError("cost overflow")

    monkeypatch.setattr(report, "estimate_cost", fail_estimation)
    with caplog.at_level(logging.ERROR, logger="src.ops_observability_report"):
        response = _get(client, tenant, kb, "1h")

    assert response.status_code == 200
    point = response.json()["series"][0]
    assert point["e2e_p95_ms"] == 100
    assert point["total_tokens"] == 100
    assert point["estimated_cost_usd"] is None
    assert point["pricing_configured"] is False
    assert point["pricing_complete"] is False
    assert point["priced_tokens"] == 0
    assert point["unpriced_tokens"] == 100
    assert "cost estimation failed" in caplog.text
