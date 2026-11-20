from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

ALLOWED_STAGES = {"rewrite", "retrieve", "rerank", "answer", "other"}
ALLOWED_SPAN_STATUS = {"ok", "error", "cancelled"}
ALLOWED_OUTCOMES = {"succeeded", "refused", "error", "cancelled"}
_ALLOWED_OPERATIONS = {"search", "chat"}


_HTTP_INSERT = text(
    """
    INSERT INTO ops_http_metrics (
        tenant_id, kb_id, route, method, status_code, duration_ms, trace_id
    ) VALUES (
        :tenant_id, :kb_id, :route, :method, :status_code, :duration_ms, :trace_id
    )
    """
)

_RAG_INSERT = text(
    """
    INSERT INTO ops_rag_metrics (
        trace_id, tenant_id, kb_id, operation, mode, outcome, hits,
        citation_ok, rerank_degraded, total_ms, rewrite_ms, retrieval_ms,
        rerank_ms, llm_ms, model, prompt_tokens, completion_tokens, error_kind,
        stage_spans
    ) VALUES (
        :trace_id, :tenant_id, :kb_id, :operation, :mode, :outcome, :hits,
        :citation_ok, :rerank_degraded, :total_ms, :rewrite_ms, :retrieval_ms,
        :rerank_ms, :llm_ms, :model, :prompt_tokens, :completion_tokens, :error_kind,
        :stage_spans
    )
    ON CONFLICT (tenant_id, trace_id) DO NOTHING
    """
).bindparams(bindparam("stage_spans", type_=JSONB))


def validate_stage_spans(spans: list[dict]) -> list[dict]:
    if not isinstance(spans, list) or len(spans) > 16:
        raise ValueError("stage_spans must be a list with at most 16 items")
    validated = []
    for span in spans:
        if set(span) != {"stage", "start_ms", "duration_ms", "status"}:
            raise ValueError("invalid stage span keys")
        if span["stage"] not in ALLOWED_STAGES or span["status"] not in ALLOWED_SPAN_STATUS:
            raise ValueError("invalid stage or status")
        start, duration = float(span["start_ms"]), float(span["duration_ms"])
        if not all(math.isfinite(value) and value >= 0 for value in (start, duration)):
            raise ValueError("stage span values must be finite and non-negative")
        validated.append({
            "stage": span["stage"], "start_ms": round(start, 3),
            "duration_ms": round(duration, 3), "status": span["status"],
        })
    return validated


def _finite_non_negative(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite and non-negative") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return number


def _optional_finite_non_negative(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite_non_negative(value, field)


def _non_negative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _limited_string(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{field} must be a string of at most {max_length} characters")
    return value


def _optional_limited_string(value: Any, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _limited_string(value, field, max_length)


def record_http_metric(engine: Engine, **fields: Any) -> None:
    status_code = fields["status_code"]
    if type(status_code) is not int or not 100 <= status_code <= 599:
        raise ValueError("status_code must be an integer between 100 and 599")
    params = {
        "tenant_id": fields["tenant_id"],
        "kb_id": fields.get("kb_id"),
        "route": _limited_string(fields["route"], "route", 256),
        "method": fields["method"],
        "status_code": status_code,
        "duration_ms": _finite_non_negative(fields["duration_ms"], "duration_ms"),
        "trace_id": fields.get("trace_id"),
    }
    with engine.begin() as connection:
        connection.execute(_HTTP_INSERT, params)


def record_rag_metric(engine: Engine, **fields: Any) -> None:
    operation = fields["operation"]
    if operation not in _ALLOWED_OPERATIONS:
        raise ValueError("invalid operation")
    outcome = fields["outcome"]
    if outcome not in ALLOWED_OUTCOMES:
        raise ValueError("invalid outcome")
    hits = _non_negative_integer(fields["hits"], "hits")
    total_ms = _finite_non_negative(fields["total_ms"], "total_ms")
    stage_spans = validate_stage_spans(fields["stage_spans"])

    params = {
        "trace_id": fields["trace_id"],
        "tenant_id": fields["tenant_id"],
        "kb_id": fields["kb_id"],
        "operation": operation,
        "mode": _limited_string(fields["mode"], "mode", 64),
        "outcome": outcome,
        "hits": hits,
        "citation_ok": fields.get("citation_ok"),
        "rerank_degraded": fields.get("rerank_degraded"),
        "total_ms": total_ms,
        "model": _optional_limited_string(fields.get("model"), "model", 256),
        "error_kind": _optional_limited_string(
            fields.get("error_kind"), "error_kind", 128
        ),
        "stage_spans": stage_spans,
    }
    for field in ("rewrite_ms", "retrieval_ms", "rerank_ms", "llm_ms"):
        params[field] = _optional_finite_non_negative(fields.get(field), field)
    for field in ("prompt_tokens", "completion_tokens"):
        value = fields.get(field)
        params[field] = None if value is None else _non_negative_integer(value, field)

    with engine.begin() as connection:
        connection.execute(_RAG_INSERT, params)


def try_record_http_metric(engine: Engine, **fields: Any) -> None:
    try:
        record_http_metric(engine, **fields)
    except Exception:  # noqa: BLE001
        logger.exception("failed to record HTTP metric")


def try_record_rag_metric(engine: Engine, **fields: Any) -> None:
    try:
        record_rag_metric(engine, **fields)
    except Exception:  # noqa: BLE001
        logger.exception("failed to record RAG metric")
