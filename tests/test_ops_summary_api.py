"""驾驶舱摘要接口测试：结构完整、租户隔离、parser 未配置不探测网络。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine
from src.ingest_jobs import enqueue
from src.ops_observability import record_rag_metric

TENANT = "t_ops_summary"
OTHER_TENANT = "t_ops_summary_other"
KB = "kb_ops_summary"
OTHER_KB = "kb_ops_summary_other"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过摘要接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops summary')"),
            {"t": TENANT},
        )
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'ops summary other')"),
            {"t": OTHER_TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'summary kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'other kb', '', 'active')"
            ),
            {"kb": OTHER_KB, "t": OTHER_TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_http_metrics WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM ops_rag_metrics WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )


@pytest.fixture()
def client(engine, monkeypatch) -> TestClient:
    # parser 未配置：避免测试发起真实网络探测
    monkeypatch.delenv("PARSER_BASE_URL", raising=False)
    monkeypatch.delenv("OPS_MODEL_PRICING_JSON", raising=False)
    return TestClient(create_app())


def _record_rag(engine, trace_id: str, **overrides) -> None:
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
        engine, trace_id=trace_id, tenant_id=TENANT, kb_id=KB, **fields
    )


def test_summary_returns_cockpit_structure(
    client: TestClient, engine, monkeypatch
) -> None:
    def unexpected_trace_scan(*args, **kwargs):
        raise AssertionError("summary must not scan JSONL traces")

    monkeypatch.setattr("src.trace.TraceStore.list_records", unexpected_trace_scan)
    enqueue(engine, TENANT, KB, doc_id="doc_ops_summary_1")

    response = client.get(f"/v1/ops/summary?kb_id={KB}", headers={"X-Tenant-Id": TENANT})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "health", "queue", "ai", "latest_runs", "latest_traces", "workers",
        "observability",
    }
    assert body["health"]["parser"] == "unconfigured"
    assert body["health"]["api"] == "up"
    assert isinstance(body["queue"]["total"], int)
    assert body["queue"]["total"] >= 1
    assert isinstance(body["ai"]["requests"], int)
    assert body["ai"]["requests"] == 0
    observability = body["observability"]
    assert set(observability) == {
        "window", "start_at", "end_at", "generated_at", "http", "rag", "tokens"
    }
    assert observability["window"] == "24h"
    assert observability["http"]["requests"] == 0
    assert observability["rag"]["operations"] == 0
    assert "tokens" not in observability["rag"]
    assert "cost" not in observability["rag"]
    assert set(observability["tokens"]) == {
        "prompt", "completion", "total", "pricing_configured", "pricing_complete",
        "estimated_cost_usd", "priced_tokens", "unpriced_tokens",
    }
    assert body["latest_traces"] == []
    assert isinstance(body["latest_runs"], list)
    assert isinstance(body["workers"], list)


def test_summary_returns_404_for_other_tenant_kb(client: TestClient) -> None:
    response = client.get(
        f"/v1/ops/summary?kb_id={OTHER_KB}", headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 404


def test_summary_returns_404_when_kb_id_belongs_to_other_tenant(client: TestClient) -> None:
    response = client.get(
        f"/v1/ops/summary?kb_id={KB}", headers={"X-Tenant-Id": OTHER_TENANT}
    )

    assert response.status_code == 404


def test_summary_maps_legacy_ai_and_returns_latest_metric_rows(
    client: TestClient, engine, monkeypatch
) -> None:
    monkeypatch.setenv(
        "OPS_MODEL_PRICING_JSON",
        '{"m1": {"prompt_per_1m": 1, "completion_per_1m": 2}}',
    )
    _record_rag(
        engine, "summary-chat-ok", hits=0, model="m1", prompt_tokens=100,
        completion_tokens=50, llm_ms=30, citation_ok=True, rerank_degraded=False,
    )
    _record_rag(
        engine, "summary-chat-refused", outcome="refused", hits=0, model="m1",
        prompt_tokens=40, completion_tokens=0,
    )
    _record_rag(
        engine, "summary-search", operation="search", hits=3,
        rerank_ms=20, rerank_degraded=True,
    )

    response = client.get(
        "/v1/ops/summary", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ai"] == {
        "requests": 2,
        "refusal_rate": 0.5,
        "zero_result_rate": 0.6667,
        "citation_ok_rate": 1.0,
        "rerank_degraded_rate": 0.5,
        "tokens": {"prompt": 140, "completion": 50, "total": 190},
        "llm_latency_ms": {"p50": 30.0, "p95": 30.0},
        "model": "m1",
        "estimated_cost": 0.00024,
    }
    assert body["observability"]["rag"]["operations"] == 3
    assert body["observability"]["tokens"] == {
        "prompt": 140,
        "completion": 50,
        "total": 190,
        "pricing_configured": True,
        "pricing_complete": True,
        "estimated_cost_usd": 0.00024,
        "priced_tokens": 190,
        "unpriced_tokens": 0,
    }
    assert len(body["latest_traces"]) == 3
    assert {row["trace_id"] for row in body["latest_traces"]} == {
        "summary-chat-ok", "summary-chat-refused", "summary-search"
    }
    assert body["latest_traces"][0]["trace_id"] == "summary-search"
    assert body["latest_traces"][0]["operation"] == "search"
    assert body["latest_traces"][0]["stage_spans"] == []


def test_summary_accepts_supported_window_and_rejects_invalid_window(
    client: TestClient,
) -> None:
    headers = {"X-Tenant-Id": TENANT}
    valid = client.get(
        "/v1/ops/summary", params={"kb_id": KB, "window": "1h"}, headers=headers
    )
    invalid = client.get(
        "/v1/ops/summary", params={"kb_id": KB, "window": "1m"}, headers=headers
    )

    assert valid.status_code == 200
    assert valid.json()["observability"]["window"] == "1h"
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "invalid window"


def test_summary_invalid_pricing_degrades_cost_without_failing(
    client: TestClient, engine, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("OPS_MODEL_PRICING_JSON", "{")
    _record_rag(engine, "summary-invalid-pricing", model="m1", prompt_tokens=100)

    with caplog.at_level(logging.ERROR, logger="app.routers.ops"):
        response = client.get(
            "/v1/ops/summary", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
        )

    assert response.status_code == 200
    tokens = response.json()["observability"]["tokens"]
    assert tokens["pricing_configured"] is False
    assert tokens["pricing_complete"] is False
    assert tokens["estimated_cost_usd"] is None
    assert response.json()["ai"]["estimated_cost"] is None
    assert "invalid model pricing configuration" in caplog.text
