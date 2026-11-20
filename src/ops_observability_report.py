"""按时间窗聚合原始运维指标，并提供摘要所需的最近 RAG 行。"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.ops_pricing import estimate_cost

logger = logging.getLogger(__name__)

WINDOWS = {
    "1h": (timedelta(hours=1), 60),
    "24h": (timedelta(hours=24), 300),
    "7d": (timedelta(days=7), 3600),
    "30d": (timedelta(days=30), 21600),
}

_HTTP_AGGREGATE = text(
    """
    SELECT
        COUNT(*)::int AS requests,
        COUNT(*) FILTER (WHERE status_code BETWEEN 400 AND 499)::int AS client_errors,
        COUNT(*) FILTER (WHERE status_code BETWEEN 500 AND 599)::int AS server_errors,
        COUNT(duration_ms)::int AS duration_samples,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms) AS duration_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS duration_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) AS duration_p99
    FROM ops_http_metrics
    WHERE tenant_id = :tenant_id
      AND kb_id = :kb_id
      AND occurred_at >= :start
      AND occurred_at < :end
    """
)

_RAG_AGGREGATE = text(
    """
    WITH scoped AS (
        SELECT id, occurred_at, operation, outcome, hits, citation_ok,
               rerank_degraded, total_ms, retrieval_ms, rerank_ms, llm_ms,
               model, prompt_tokens, completion_tokens
        FROM ops_rag_metrics
        WHERE tenant_id = :tenant_id
          AND kb_id = :kb_id
          AND occurred_at >= :start
          AND occurred_at < :end
    ),
    model_tokens AS (
        SELECT model,
               COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens
        FROM scoped
        GROUP BY model
    )
    SELECT
        COUNT(*)::int AS operations,
        COUNT(*) FILTER (
            WHERE hits = 0 AND outcome IN ('succeeded', 'refused')
        )::int AS zero_hit_numerator,
        COUNT(*) FILTER (
            WHERE outcome IN ('succeeded', 'refused')
        )::int AS zero_hit_denominator,
        COUNT(*) FILTER (
            WHERE operation = 'chat' AND outcome = 'refused'
        )::int AS refusal_numerator,
        COUNT(*) FILTER (
            WHERE operation = 'chat' AND outcome IN ('succeeded', 'refused')
        )::int AS refusal_denominator,
        COUNT(*) FILTER (WHERE rerank_degraded IS TRUE)::int AS rerank_degraded_numerator,
        COUNT(rerank_degraded)::int AS rerank_degraded_denominator,
        COUNT(*) FILTER (WHERE citation_ok IS TRUE)::int AS citation_numerator,
        COUNT(citation_ok)::int AS citation_denominator,
        COUNT(total_ms)::int AS e2e_samples,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY total_ms) AS e2e_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS e2e_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY total_ms) AS e2e_p99,
        COUNT(retrieval_ms)::int AS retrieve_samples,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY retrieval_ms)
            FILTER (WHERE retrieval_ms IS NOT NULL) AS retrieve_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY retrieval_ms)
            FILTER (WHERE retrieval_ms IS NOT NULL) AS retrieve_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY retrieval_ms)
            FILTER (WHERE retrieval_ms IS NOT NULL) AS retrieve_p99,
        COUNT(rerank_ms)::int AS rerank_samples,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY rerank_ms)
            FILTER (WHERE rerank_ms IS NOT NULL) AS rerank_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY rerank_ms)
            FILTER (WHERE rerank_ms IS NOT NULL) AS rerank_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY rerank_ms)
            FILTER (WHERE rerank_ms IS NOT NULL) AS rerank_p99,
        COUNT(llm_ms)::int AS llm_samples,
        percentile_cont(0.50) WITHIN GROUP (ORDER BY llm_ms)
            FILTER (WHERE llm_ms IS NOT NULL) AS llm_p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ms)
            FILTER (WHERE llm_ms IS NOT NULL) AS llm_p95,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY llm_ms)
            FILTER (WHERE llm_ms IS NOT NULL) AS llm_p99,
        COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens,
        COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens,
        (array_agg(model ORDER BY occurred_at DESC, id DESC)
            FILTER (WHERE model IS NOT NULL))[1] AS model,
        (
            SELECT COALESCE(
                json_agg(json_build_object(
                    'model', model,
                    'prompt_tokens', prompt_tokens,
                    'completion_tokens', completion_tokens
                )), '[]'::json
            )
            FROM model_tokens
        ) AS model_tokens
    FROM scoped
    """
)

_HTTP_TIMESERIES = text(
    """
    WITH scoped AS (
        SELECT
            to_timestamp(
                floor(extract(epoch from occurred_at) / :bucket_seconds)
                * :bucket_seconds
            ) AS bucket,
            status_code
        FROM ops_http_metrics
        WHERE tenant_id = :tenant_id
          AND kb_id = :kb_id
          AND occurred_at >= :start
          AND occurred_at < :end
    ), bounded AS (
        SELECT LEAST(bucket, to_timestamp(:last_bucket_epoch)) AS bucket, status_code
        FROM scoped
    )
    SELECT
        extract(epoch from bucket)::bigint AS bucket_epoch,
        COUNT(*)::int AS requests,
        COUNT(*) FILTER (WHERE status_code BETWEEN 400 AND 499)::int AS client_errors,
        COUNT(*) FILTER (WHERE status_code BETWEEN 500 AND 599)::int AS server_errors
    FROM bounded
    GROUP BY bucket
    """
)

_RAG_TIMESERIES = text(
    """
    WITH scoped AS (
        SELECT
            to_timestamp(
                floor(extract(epoch from occurred_at) / :bucket_seconds)
                * :bucket_seconds
            ) AS bucket,
            total_ms, retrieval_ms, llm_ms, model, prompt_tokens, completion_tokens
        FROM ops_rag_metrics
        WHERE tenant_id = :tenant_id
          AND kb_id = :kb_id
          AND occurred_at >= :start
          AND occurred_at < :end
    ), bounded AS (
        SELECT LEAST(bucket, to_timestamp(:last_bucket_epoch)) AS bucket,
               total_ms, retrieval_ms, llm_ms, model, prompt_tokens, completion_tokens
        FROM scoped
    ), bucket_metrics AS (
        SELECT
            bucket,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) AS e2e_p95,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY retrieval_ms)
                FILTER (WHERE retrieval_ms IS NOT NULL) AS retrieval_p95,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY llm_ms)
                FILTER (WHERE llm_ms IS NOT NULL) AS llm_p95,
            COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens
        FROM bounded
        GROUP BY bucket
    ), model_tokens AS (
        SELECT bucket, model,
               COALESCE(SUM(prompt_tokens), 0)::bigint AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0)::bigint AS completion_tokens
        FROM bounded
        GROUP BY bucket, model
    )
    SELECT
        extract(epoch from bucket)::bigint AS bucket_epoch,
        e2e_p95, retrieval_p95, llm_p95, prompt_tokens, completion_tokens,
        COALESCE((
            SELECT json_agg(json_build_object(
                'model', model,
                'prompt_tokens', prompt_tokens,
                'completion_tokens', completion_tokens
            ) ORDER BY model)
            FROM model_tokens
            WHERE model_tokens.bucket = bucket_metrics.bucket
        ), '[]'::json) AS model_tokens
    FROM bucket_metrics
    """
)

_LATEST_RAG_ROWS = """
    SELECT id, trace_id, occurred_at, operation, mode, outcome, hits,
           citation_ok, rerank_degraded, total_ms, rewrite_ms, retrieval_ms,
           rerank_ms, llm_ms, model, prompt_tokens, completion_tokens,
           error_kind, stage_spans
    FROM ops_rag_metrics
    WHERE tenant_id = :tenant_id AND kb_id = :kb_id
"""


def resolve_window(
    name: str, *, now: datetime | None = None
) -> tuple[datetime, datetime, int]:
    if name not in WINDOWS:
        raise ValueError("invalid window")
    delta, bucket_seconds = WINDOWS[name]
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return end - delta, end, bucket_seconds


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _rate(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": _ratio(numerator, denominator),
    }


def _quantiles(row, prefix: str) -> dict:
    return {
        "p50": row[f"{prefix}_p50"],
        "p95": row[f"{prefix}_p95"],
        "p99": row[f"{prefix}_p99"],
        "samples": row[f"{prefix}_samples"],
    }


def _cost(model_tokens: list[dict], pricing: dict, total_tokens: int) -> dict:
    try:
        return estimate_cost(model_tokens, pricing)
    except ValueError:
        logger.exception("cost estimation failed")
        return {
            "pricing_configured": False,
            "pricing_complete": False,
            "estimated_cost_usd": None,
            "priced_tokens": 0,
            "unpriced_tokens": total_tokens,
        }


def summarize_observability(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    window: str,
    pricing: dict,
    now: datetime | None = None,
) -> dict:
    start, end, _ = resolve_window(window, now=now)
    params = {"tenant_id": tenant_id, "kb_id": kb_id, "start": start, "end": end}
    with engine.connect() as connection:
        http_row = connection.execute(_HTTP_AGGREGATE, params).mappings().one()
        rag_row = connection.execute(_RAG_AGGREGATE, params).mappings().one()

    requests = http_row["requests"]
    window_minutes = (end - start).total_seconds() / 60
    prompt_tokens = rag_row["prompt_tokens"]
    completion_tokens = rag_row["completion_tokens"]
    total_tokens = prompt_tokens + completion_tokens
    return {
        "window": window,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "generated_at": end.isoformat(),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": total_tokens,
            **_cost(rag_row["model_tokens"], pricing, total_tokens),
        },
        "http": {
            "requests": requests,
            "avg_qpm": round(requests / window_minutes, 4),
            "4xx_rate": _ratio(http_row["client_errors"], requests),
            "5xx_rate": _ratio(http_row["server_errors"], requests),
            "duration_ms": _quantiles(http_row, "duration"),
        },
        "rag": {
            "operations": rag_row["operations"],
            "zero_hit": _rate(
                rag_row["zero_hit_numerator"], rag_row["zero_hit_denominator"]
            ),
            "refusal": _rate(
                rag_row["refusal_numerator"], rag_row["refusal_denominator"]
            ),
            "rerank_degraded": _rate(
                rag_row["rerank_degraded_numerator"],
                rag_row["rerank_degraded_denominator"],
            ),
            "citation": _rate(
                rag_row["citation_numerator"], rag_row["citation_denominator"]
            ),
            "latency_ms": {
                prefix: _quantiles(rag_row, prefix)
                for prefix in ("e2e", "retrieve", "rerank", "llm")
            },
            "model": rag_row["model"],
        },
    }


def build_observability_timeseries(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    window: str,
    pricing: dict,
    now: datetime | None = None,
) -> dict:
    start, end, bucket_seconds = resolve_window(window, now=now)
    bucket_count = int((end - start).total_seconds()) // bucket_seconds
    first_epoch = math.floor(start.timestamp() / bucket_seconds) * bucket_seconds
    last_bucket_epoch = first_epoch + (bucket_count - 1) * bucket_seconds
    params = {
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "start": start,
        "end": end,
        "bucket_seconds": bucket_seconds,
        "last_bucket_epoch": last_bucket_epoch,
    }
    with engine.connect() as connection:
        http_rows = connection.execute(_HTTP_TIMESERIES, params).mappings().all()
        rag_rows = connection.execute(_RAG_TIMESERIES, params).mappings().all()

    points_by_epoch = {}
    series = []
    for index in range(bucket_count):
        bucket_epoch = first_epoch + index * bucket_seconds
        point = {
            "ts": datetime.fromtimestamp(bucket_epoch, timezone.utc).isoformat(),
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
        points_by_epoch[bucket_epoch] = point
        series.append(point)

    for row in http_rows:
        point = points_by_epoch[row["bucket_epoch"]]
        point["requests"] = row["requests"]
        point["client_errors"] = row["client_errors"]
        point["server_errors"] = row["server_errors"]

    for row in rag_rows:
        point = points_by_epoch[row["bucket_epoch"]]
        total_tokens = row["prompt_tokens"] + row["completion_tokens"]
        point.update({
            "e2e_p95_ms": row["e2e_p95"],
            "retrieval_p95_ms": row["retrieval_p95"],
            "llm_p95_ms": row["llm_p95"],
            "total_tokens": total_tokens,
            **_cost(row["model_tokens"], pricing, total_tokens),
        })

    return {
        "window": window,
        "bucket_seconds": bucket_seconds,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "series": series,
    }


def list_rag_metric_rows(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    *,
    limit: int = 8,
    window: str | None = None,
    outcome: str | None = None,
    operation: str | None = None,
    mode: str | None = None,
    min_ms: float | None = None,
    trace_id: str | None = None,
    before_occurred_at: datetime | None = None,
    before_id: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    params = {"tenant_id": tenant_id, "kb_id": kb_id, "limit": limit}
    filters = []
    if window is not None:
        start, end, _ = resolve_window(window, now=now)
        params.update({"start": start, "end": end})
        filters.append(" AND occurred_at >= :start AND occurred_at < :end")
    for name, value in (
        ("outcome", outcome),
        ("operation", operation),
        ("mode", mode),
        ("trace_id", trace_id),
    ):
        if value is not None:
            params[name] = value
            filters.append(f" AND {name} = :{name}")
    if min_ms is not None:
        params["min_ms"] = min_ms
        filters.append(" AND total_ms >= :min_ms")
    if (before_occurred_at is None) != (before_id is None):
        raise ValueError("cursor parameters must be provided together")
    if before_occurred_at is not None:
        params.update({
            "before_occurred_at": before_occurred_at,
            "before_id": before_id,
        })
        filters.append(
            " AND (occurred_at < :before_occurred_at"
            " OR (occurred_at = :before_occurred_at AND id < :before_id))"
        )

    query = _LATEST_RAG_ROWS + "\n".join(filters)
    query += "\nORDER BY occurred_at DESC, id DESC\nLIMIT :limit"
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).mappings().all()
    return [dict(row) for row in rows]
