from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from src.ops_observability import (
    record_http_metric,
    record_rag_metric,
    try_record_http_metric,
    try_record_rag_metric,
    validate_stage_spans,
)


NON_FINITE = [float("nan"), float("inf"), float("-inf")]


def valid_rag_fields(tenant: str, kb: str) -> dict:
    return {
        "trace_id": "tr_metric_test",
        "tenant_id": tenant,
        "kb_id": kb,
        "operation": "search",
        "mode": "hybrid",
        "outcome": "succeeded",
        "hits": 1,
        "total_ms": 10.0,
        "stage_spans": [
            {"stage": "retrieve", "start_ms": 0.0, "duration_ms": 10.0, "status": "ok"}
        ],
    }


def valid_http_fields(tenant: str, kb: str) -> dict:
    return {
        "tenant_id": tenant,
        "kb_id": kb,
        "route": "/api/search",
        "method": "POST",
        "status_code": 200,
        "duration_ms": 12.5,
        "trace_id": "tr_http_metric_test",
    }


def test_validate_stage_spans_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_stage_spans([
            {"stage": "retrieve", "start_ms": 0, "duration_ms": float("nan"), "status": "ok"}
        ])


@pytest.mark.parametrize("value", NON_FINITE)
def test_validate_stage_spans_rejects_non_finite_start(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_stage_spans([
            {"stage": "retrieve", "start_ms": value, "duration_ms": 0, "status": "ok"}
        ])


def test_validate_stage_spans_rejects_more_than_16() -> None:
    spans = [
        {"stage": "other", "start_ms": index, "duration_ms": 1, "status": "ok"}
        for index in range(17)
    ]
    with pytest.raises(ValueError, match="at most 16"):
        validate_stage_spans(spans)


@pytest.mark.parametrize(
    ("key", "value"),
    [("stage", "unknown"), ("status", "unknown"), ("start_ms", -1), ("duration_ms", -1)],
)
def test_validate_stage_spans_rejects_invalid_entries(key: str, value) -> None:
    span = {"stage": "retrieve", "start_ms": 0, "duration_ms": 1, "status": "ok"}
    span[key] = value
    with pytest.raises(ValueError):
        validate_stage_spans([span])


@pytest.mark.parametrize("key_to_remove", ["stage", "start_ms", "duration_ms", "status"])
def test_validate_stage_spans_rejects_invalid_keys(key_to_remove: str) -> None:
    span = {"stage": "retrieve", "start_ms": 0, "duration_ms": 1, "status": "ok"}
    del span[key_to_remove]
    with pytest.raises(ValueError, match="keys"):
        validate_stage_spans([span])


def test_http_metric_insert_persists_bound_fields(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    fields = valid_http_fields(tenant, kb)

    record_http_metric(engine, **fields)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT tenant_id, kb_id, route, method, status_code, duration_ms, trace_id "
                "FROM ops_http_metrics WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant},
        ).mappings().one()
    assert dict(row) == fields


def test_rag_metric_insert_persists_all_fields(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    fields = valid_rag_fields(tenant, kb)
    fields.update({
        "citation_ok": True,
        "rerank_degraded": False,
        "rewrite_ms": 1.0,
        "retrieval_ms": 2.0,
        "rerank_ms": 3.0,
        "llm_ms": 4.0,
        "model": "test-model",
        "prompt_tokens": 0,
        "completion_tokens": 5,
        "error_kind": None,
    })

    record_rag_metric(engine, **fields)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT trace_id, tenant_id, kb_id, operation, mode, outcome, hits, "
                "citation_ok, rerank_degraded, total_ms, rewrite_ms, retrieval_ms, "
                "rerank_ms, llm_ms, model, prompt_tokens, completion_tokens, error_kind, "
                "stage_spans FROM ops_rag_metrics WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant},
        ).mappings().one()
    assert dict(row) == fields


def test_rag_metric_insert_is_idempotent(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    fields = valid_rag_fields(tenant, kb)
    record_rag_metric(engine, **fields)
    record_rag_metric(engine, **fields)
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM ops_rag_metrics WHERE tenant_id = :t"),
            {"t": tenant},
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("operation", "delete"),
        ("outcome", "unknown"),
        ("hits", -1),
        ("hits", 1.5),
        ("hits", True),
        ("total_ms", -1),
        ("total_ms", float("nan")),
        ("total_ms", float("inf")),
        ("total_ms", float("-inf")),
        ("stage_spans", {}),
    ],
)
def test_rag_metric_rejects_invalid_required_fields(key: str, value) -> None:
    fields = valid_rag_fields("tenant", "kb")
    fields[key] = value
    with pytest.raises(ValueError):
        record_rag_metric(object(), **fields)


@pytest.mark.parametrize("key", ["rewrite_ms", "retrieval_ms", "rerank_ms", "llm_ms"])
@pytest.mark.parametrize("value", [-0.01, *NON_FINITE])
def test_rag_metric_rejects_invalid_optional_numbers(key: str, value: float) -> None:
    fields = valid_rag_fields("tenant", "kb")
    fields[key] = value
    with pytest.raises(ValueError):
        record_rag_metric(object(), **fields)


@pytest.mark.parametrize("key", ["prompt_tokens", "completion_tokens"])
@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_rag_metric_rejects_invalid_tokens(key: str, value) -> None:
    fields = valid_rag_fields("tenant", "kb")
    fields[key] = value
    with pytest.raises(ValueError):
        record_rag_metric(object(), **fields)


@pytest.mark.parametrize(("key", "length"), [("mode", 65), ("model", 257), ("error_kind", 129)])
def test_rag_metric_rejects_overlong_strings(key: str, length: int) -> None:
    fields = valid_rag_fields("tenant", "kb")
    fields[key] = "x" * length
    with pytest.raises(ValueError, match=key):
        record_rag_metric(object(), **fields)


def test_http_metric_rejects_overlong_route() -> None:
    fields = valid_http_fields("tenant", "kb")
    fields["route"] = "x" * 257
    with pytest.raises(ValueError, match="route"):
        record_http_metric(object(), **fields)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status_code", 99),
        ("status_code", 600),
        ("status_code", 200.5),
        ("duration_ms", -1),
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
    ],
)
def test_http_metric_rejects_invalid_required_fields(key: str, value) -> None:
    fields = valid_http_fields("tenant", "kb")
    fields[key] = value
    with pytest.raises(ValueError):
        record_http_metric(object(), **fields)


class FailingEngine:
    def begin(self):
        raise RuntimeError("database unavailable")


def test_try_record_http_metric_logs_write_failure(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="src.ops_observability"):
        try_record_http_metric(FailingEngine(), **valid_http_fields("tenant", "kb"))
    assert "database unavailable" in caplog.text
    assert caplog.records[0].exc_info is not None


def test_try_record_rag_metric_logs_write_failure(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="src.ops_observability"):
        try_record_rag_metric(FailingEngine(), **valid_rag_fields("tenant", "kb"))
    assert "database unavailable" in caplog.text
    assert caplog.records[0].exc_info is not None
