"""压测脚本的协议、并发与统计口径测试；所有 HTTP 均由内存 transport 提供。"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _loadtest():
    import loadtest

    return loadtest


def _chat_body(degraded=False):
    hits = json.dumps({"chunks": [], "rerank_degraded": degraded})
    return (
        f"event: hits\ndata: {hits}\n\n"
        'data: {"delta":"answer"}\n\n'
        "data: [DONE]\n\n"
    )


def _run(loadtest, handler, **overrides):
    options = {
        "base_url": "http://testserver",
        "kb_id": "kb_demo",
        "tenant_id": "tenant_a",
        "query": "退款多久到账",
        "concurrency": 2,
        "requests": 1,
        "transport": httpx.MockTransport(handler),
    }
    options.update(overrides)
    return asyncio.run(loadtest.run_loadtest(**options))


def test_percentile_uses_fixed_linear_interpolation_and_boundaries():
    loadtest = _loadtest()
    values = [30, 0, 20, 10]

    assert loadtest.percentile(values, 0) == 0
    assert loadtest.percentile(values, 0.5) == 15
    assert loadtest.percentile(values, 0.95) == pytest.approx(28.5)
    assert loadtest.percentile(values, 1) == 30
    assert loadtest.percentile([12], 0.99) == 12
    assert loadtest.percentile([], 0.5) is None


@pytest.mark.parametrize("quantile", [-0.01, 1.01])
def test_percentile_rejects_quantiles_outside_unit_interval(quantile):
    loadtest = _loadtest()
    with pytest.raises(ValueError):
        loadtest.percentile([1], quantile)


def test_empty_summary_has_null_rates_and_latencies():
    loadtest = _loadtest()

    summary = loadtest.summarize_results([])

    assert summary["total"] == 0
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert summary["error_rate"] is None
    assert summary["latency_ms"] == {
        "sample_count": 0,
        "p50": None,
        "p95": None,
        "p99": None,
    }
    assert summary["errors"] == []


def test_summary_error_rate_and_latency_include_failed_samples():
    loadtest = _loadtest()
    results = [
        {"success": True, "latency_ms": 10.0},
        {
            "success": False,
            "latency_ms": 20.0,
            "error_type": "http_500",
            "error": "HTTP 500",
        },
    ]

    summary = loadtest.summarize_results(results)

    assert summary["total"] == 2
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["error_rate"] == 0.5
    assert summary["latency_ms"]["sample_count"] == 2
    assert summary["latency_ms"]["p50"] == 15
    assert summary["error_counts"] == {"http_500": 1}


def test_run_loadtest_uses_real_http_paths_payloads_and_tenant_headers():
    loadtest = _loadtest()
    seen = []

    async def handler(request: httpx.Request):
        seen.append(
            {
                "path": request.url.path,
                "tenant": request.headers.get("X-Tenant-Id"),
                "user": request.headers.get("X-User-Id"),
                "json": json.loads(request.content),
            }
        )
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})
        return httpx.Response(200, text=_chat_body())

    report = _run(loadtest, handler, user_id="user_7", top_k=7)

    assert {item["path"] for item in seen} == {
        "/v1/kbs/kb_demo/search",
        "/v1/chat/stream",
    }
    search = next(item for item in seen if item["path"].endswith("/search"))
    chat = next(item for item in seen if item["path"].endswith("/stream"))
    assert search["json"] == {
        "query": "退款多久到账",
        "top_k": 7,
        "mode": "hybrid",
        "audience": "public",
    }
    assert chat["json"] == {
        "kb_id": "kb_demo",
        "question": "退款多久到账",
        "history": [],
        "top_k": 7,
        "audience": "public",
    }
    assert all(item["tenant"] == "tenant_a" for item in seen)
    assert all(item["user"] == "user_7" for item in seen)
    assert all("tenant_id" not in item["json"] for item in seen)
    assert report["config"]["requests_per_endpoint"] == 1
    assert report["config"]["total_requests"] == 2
    assert report["endpoints"]["search"]["succeeded"] == 1
    assert report["endpoints"]["chat"]["succeeded"] == 1


def test_worker_pool_reaches_but_never_exceeds_concurrency_limit():
    loadtest = _loadtest()
    active = 0
    maximum = 0

    async def handler(request: httpx.Request):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.01)
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"hits": []})
            return httpx.Response(200, text=_chat_body())
        finally:
            active -= 1

    report = _run(loadtest, handler, concurrency=3, requests=4)

    assert maximum == 3
    assert report["endpoints"]["search"]["total"] == 4
    assert report["endpoints"]["chat"]["total"] == 4
    assert report["endpoints"]["search"]["succeeded"] == 4
    assert report["endpoints"]["chat"]["succeeded"] == 4


def test_chat_success_requires_consuming_the_complete_sse_stream():
    loadtest = _loadtest()
    consumed = []
    exhausted = False

    async def handler(request: httpx.Request):
        nonlocal exhausted
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})

        async def body():
            nonlocal exhausted
            for chunk in (
                b"event: hits\n",
                b'data: {"chunks":[],"rerank_degraded":false}\n\n',
                b'data: {"delta":"slow final delta"}\n\n',
                b"data: [DONE]\n\n",
            ):
                await asyncio.sleep(0)
                consumed.append(chunk)
                yield chunk
            exhausted = True

        return httpx.Response(200, content=body())

    report = _run(loadtest, handler)

    assert exhausted is True
    assert len(consumed) == 4
    assert report["endpoints"]["chat"]["succeeded"] == 1
    assert report["endpoints"]["chat"]["latency_ms"]["sample_count"] == 1


def test_sse_error_and_missing_done_are_failures_not_discarded_samples():
    loadtest = _loadtest()
    chat_calls = 0

    async def handler(request: httpx.Request):
        nonlocal chat_calls
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})
        chat_calls += 1
        if chat_calls == 1:
            body = "event: error\ndata: {\"message\":\"failed\"}\n\ndata: [DONE]\n\n"
        else:
            body = 'event: hits\ndata: {"rerank_degraded":true}\n\n'
        return httpx.Response(200, text=body)

    report = _run(loadtest, handler, requests=2)
    chat = report["endpoints"]["chat"]

    assert chat["total"] == 2
    assert chat["succeeded"] == 0
    assert chat["failed"] == 2
    assert chat["error_rate"] == 1
    assert chat["latency_ms"]["sample_count"] == 2
    assert chat["error_counts"] == {"missing_done": 1, "sse_error": 1}
    assert chat["rerank_degradation_rate"] is None
    assert chat["rerank_observed"] == 0


def test_rerank_degradation_rate_uses_only_successful_chat_hits():
    loadtest = _loadtest()
    chat_calls = 0

    async def handler(request: httpx.Request):
        nonlocal chat_calls
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"hits": []})
        chat_calls += 1
        if chat_calls == 3:
            body = (
                'event: hits\ndata: {"rerank_degraded":true}\n\n'
                "event: error\ndata: {\"message\":\"late failure\"}\n\n"
                "data: [DONE]\n\n"
            )
        else:
            body = _chat_body(degraded=chat_calls == 2)
        return httpx.Response(200, text=body)

    report = _run(loadtest, handler, requests=3, concurrency=1)
    search = report["endpoints"]["search"]
    chat = report["endpoints"]["chat"]

    assert search["succeeded"] == 3
    assert chat["succeeded"] == 2
    assert chat["failed"] == 1
    assert chat["rerank_degraded"] == 1
    assert chat["rerank_observed"] == 2
    assert chat["rerank_degradation_rate"] == 0.5
    assert chat["rerank_denominator"] == "successful_chat_responses_with_valid_hits_event"


def test_connection_and_http_errors_are_counted_with_latency():
    loadtest = _loadtest()

    async def handler(request: httpx.Request):
        if request.url.path.endswith("/search"):
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(503, text="unavailable")

    report = _run(loadtest, handler)
    search = report["endpoints"]["search"]
    chat = report["endpoints"]["chat"]

    assert search["failed"] == chat["failed"] == 1
    assert search["error_rate"] == chat["error_rate"] == 1
    assert search["latency_ms"]["sample_count"] == 1
    assert chat["latency_ms"]["sample_count"] == 1
    assert search["latency_ms"]["p50"] is not None
    assert chat["latency_ms"]["p50"] is not None
    assert search["error_counts"] == {"ConnectError": 1}
    assert chat["error_counts"] == {"http_503": 1}
    assert search["errors"][0]["error"]
    assert chat["errors"][0]["error"]


def test_cli_defaults_document_requests_per_endpoint():
    loadtest = _loadtest()

    args = loadtest.build_parser().parse_args([])

    assert args.concurrency == 20
    assert args.requests == 200
    assert args.out == ROOT / "eval" / "loadtest_report.json"
    assert "每个端点" in loadtest.build_parser().format_help()


def test_main_maps_cli_arguments_and_atomically_replaces_report(tmp_path, monkeypatch):
    loadtest = _loadtest()
    captured = {}
    out = tmp_path / "nested" / "report.json"
    out.parent.mkdir()
    out.write_text("old report", encoding="utf-8")

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {
            "timestamp": "2026-11-19T00:00:00+00:00",
            "config": {"requests_per_endpoint": kwargs["requests"]},
            "endpoints": {
                "search": loadtest.summarize_results([]),
                "chat": loadtest.summarize_results([]),
            },
        }

    monkeypatch.setattr(loadtest, "run_loadtest", fake_run)
    exit_code = loadtest.main(
        [
            "--base-url",
            "http://service:9000/",
            "--kb",
            "kb_load",
            "--tenant",
            "tenant_load",
            "--query",
            "保修政策",
            "--concurrency",
            "3",
            "--requests",
            "2",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    assert captured["base_url"] == "http://service:9000"
    assert captured["kb_id"] == "kb_load"
    assert captured["tenant_id"] == "tenant_load"
    assert captured["query"] == "保修政策"
    assert captured["concurrency"] == 3
    assert captured["requests"] == 2
    assert json.loads(out.read_text(encoding="utf-8"))["config"] == {
        "requests_per_endpoint": 2
    }
    assert list(out.parent.iterdir()) == [out]
