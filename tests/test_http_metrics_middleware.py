from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app import http_metrics
from app.http_metrics import HttpMetricsMiddleware


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpMetricsMiddleware)

    @app.post("/v1/kbs/{kb_id}/probe")
    async def probe(kb_id: str):
        return {"ok": True}

    @app.get("/v1/ops/summary")
    async def summary():
        return {"ok": True}

    @app.get("/v1/stream")
    async def stream():
        async def body():
            yield b"one"
            await asyncio.sleep(0.01)
            yield b"two"

        return StreamingResponse(body())

    @app.get("/v1/state")
    async def state(request: Request):
        request.state.kb_id = "kb_from_state"
        request.state.trace_id = "trace_from_state"
        return {"ok": True}

    @app.get("/v1/client-error")
    async def client_error():
        raise HTTPException(status_code=418, detail="teapot")

    @app.get("/v1/boom")
    async def boom():
        raise RuntimeError("business boom")

    @app.get("/outside")
    async def outside():
        return {"ok": True}

    return app


def collect_calls(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(http_metrics, "_write", lambda fields: calls.append(fields))
    return calls


def test_http_metric_uses_route_template_and_kb(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    response = client.post("/v1/kbs/kb_a/probe", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 200
    assert calls[0]["route"] == "/v1/kbs/{kb_id}/probe"
    assert calls[0]["kb_id"] == "kb_a"
    assert calls[0]["status_code"] == 200
    assert calls[0]["method"] == "POST"
    assert calls[0]["tenant_id"] == "t"


def test_http_metric_waits_for_sse_and_skips_ops(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    assert client.get("/v1/stream", headers={"X-Tenant-Id": "t"}).text == "onetwo"
    assert calls[0]["duration_ms"] >= 8

    client.get("/v1/ops/summary", headers={"X-Tenant-Id": "t"})
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/v1/kbs/kb_a/probe", {}),
        ("/v1/kbs/kb_a/probe", {"X-Tenant-Id": "   "}),
        ("/outside", {"X-Tenant-Id": "t"}),
        ("/v1/ops/summary", {"X-Tenant-Id": "t"}),
    ],
)
def test_http_metric_skips_uninstrumentable_requests(
    monkeypatch, path: str, headers: dict[str, str]
) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    if path.endswith("/probe"):
        response = client.post(path, headers=headers)
    else:
        response = client.get(path, headers=headers)

    assert response.status_code == 200
    assert calls == []


def test_http_metric_uses_state_fallbacks(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    response = client.get("/v1/state", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 200
    assert calls[0]["kb_id"] == "kb_from_state"
    assert calls[0]["trace_id"] == "trace_from_state"


def test_http_metric_records_4xx_with_route_template(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    response = client.get("/v1/client-error", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 418
    assert calls[0]["status_code"] == 418
    assert calls[0]["route"] == "/v1/client-error"


def test_http_metric_records_unmatched_404_without_raw_path(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app())

    response = client.get("/v1/missing/high-cardinality", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 404
    assert calls[0]["status_code"] == 404
    assert calls[0]["route"] == "unmatched"
    assert calls[0]["kb_id"] is None


def test_http_metric_records_5xx_with_route_template(monkeypatch) -> None:
    calls = collect_calls(monkeypatch)
    client = TestClient(make_app(), raise_server_exceptions=False)

    response = client.get("/v1/boom", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 500
    assert calls[0]["status_code"] == 500
    assert calls[0]["route"] == "/v1/boom"


def test_recorder_failure_preserves_response_and_logs(monkeypatch, caplog) -> None:
    def fail_write(fields: dict) -> None:
        raise ValueError("recorder boom")

    monkeypatch.setattr(http_metrics, "_write", fail_write)
    client = TestClient(make_app())

    with caplog.at_level(logging.ERROR, logger="app.http_metrics"):
        response = client.post("/v1/kbs/kb_a/probe", headers={"X-Tenant-Id": "t"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "failed to record HTTP metric" in caplog.text
    assert any(record.exc_info is not None for record in caplog.records)


def test_recorder_failure_does_not_replace_business_exception(
    monkeypatch, caplog
) -> None:
    def fail_write(fields: dict) -> None:
        raise ValueError("recorder boom")

    monkeypatch.setattr(http_metrics, "_write", fail_write)
    client = TestClient(make_app())

    with caplog.at_level(logging.ERROR, logger="app.http_metrics"):
        with pytest.raises(RuntimeError, match="business boom"):
            client.get("/v1/boom", headers={"X-Tenant-Id": "t"})

    assert "failed to record HTTP metric" in caplog.text


def test_create_app_registers_http_metrics_middleware() -> None:
    from app.main import create_app

    app = create_app()

    assert any(
        middleware.cls is HttpMetricsMiddleware for middleware in app.user_middleware
    )
