"""请求 trace：阶段耗时、JSONL 持久化、租户隔离与 SSE 回查。"""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.deps import get_chat_client
from test_api_search import KB, TENANT, FakeChat, FailingChat, client  # noqa: F401


def _record(store, trace_id: str, *, stage: str = "retrieve", query: str = "退款"):
    return store.record(
        trace_id=trace_id,
        tenant=TENANT,
        kb=KB,
        stage=stage,
        ms=1.25,
        hits=1,
        refused=False,
        mode="hybrid",
        query=query,
    )


def _get_trace(client: TestClient, trace_id: str, tenant: str = TENANT):
    return client.get(f"/v1/traces/{trace_id}", headers={"X-Tenant-Id": tenant})


def test_store_appends_jsonl_and_reads_today_after_memory_miss(tmp_path) -> None:
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    first = _record(store, "abc123def456", query="退款多久")
    _record(store, "abc123def456", stage="answer", query="退款多久")

    path = tmp_path / f"{first['ts'][:10]}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    payloads = [json.loads(line) for line in lines]
    assert [item["stage"] for item in payloads] == ["retrieve", "answer"]
    assert all("answer" not in item for item in payloads)
    assert datetime.fromisoformat(first["ts"].replace("Z", "+00:00")).utcoffset() == timedelta(0)

    restarted = TraceStore(tmp_path)
    stages = restarted.find("abc123def456", TENANT)
    assert [item["stage"] for item in stages] == ["retrieve", "answer"]
    assert stages[0]["query"] == "退款多久"
    assert restarted.find("abc123def456", "another-tenant") == []


def test_memory_ring_keeps_latest_1000_records(tmp_path) -> None:
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    for index in range(1001):
        _record(store, f"{index:012d}")

    assert store._records.maxlen == 1000
    assert len(store._records) == 1000
    assert {item["trace_id"] for item in store._records} == {
        f"{index:012d}" for index in range(1, 1001)
    }


def test_concurrent_records_are_complete_json_lines(tmp_path) -> None:
    from src.trace import TraceStore

    store = TraceStore(tmp_path)

    def write(index: int) -> None:
        _record(store, f"{index:012d}", query=f"query-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(80)))

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    payloads = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 80
    assert {item["query"] for item in payloads} == {f"query-{index}" for index in range(80)}


def test_search_returns_trace_header_and_supports_tenant_safe_lookup(client: TestClient) -> None:
    response = client.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款 到账", "top_k": 3, "mode": "hybrid"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    trace_id = response.headers["X-Trace-Id"]
    assert re.fullmatch(r"[0-9a-f]{12}", trace_id)

    trace_response = _get_trace(client, trace_id)
    assert trace_response.status_code == 200
    body = trace_response.json()
    assert body["trace_id"] == trace_id
    assert len(body["stages"]) == 1
    stage = body["stages"][0]
    assert stage["trace_id"] == trace_id
    assert stage["tenant"] == TENANT
    assert stage["kb"] == KB
    assert stage["stage"] == "retrieve"
    assert math.isfinite(stage["ms"]) and stage["ms"] >= 0
    assert stage["hits"] == len(response.json()["hits"]) == 1
    assert stage["refused"] is False
    assert stage["mode"] == "hybrid"
    assert stage["query"] == "退款 到账"
    assert datetime.fromisoformat(stage["ts"].replace("Z", "+00:00")).utcoffset() == timedelta(0)
    assert "answer" not in stage

    assert _get_trace(client, trace_id, "another-tenant").status_code == 404
    assert _get_trace(client, "000000000000").status_code == 404


def test_chat_trace_is_available_after_stream_and_marks_answered(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", "0")
    client.app.dependency_overrides[get_chat_client] = lambda: FakeChat()

    response = client.post(
        "/v1/chat/stream",
        json={"kb_id": KB, "question": "退款多久到账"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    assert response.text.rstrip().endswith("data: [DONE]")
    trace_id = response.headers["X-Trace-Id"]
    body = _get_trace(client, trace_id).json()
    assert body["trace_id"] == trace_id
    assert len(body["stages"]) == 1
    stage = body["stages"][0]
    assert stage["stage"] == "answer"
    assert stage["mode"] == "chat"
    assert stage["query"] == "退款多久到账"
    assert stage["hits"] == 1
    assert stage["refused"] is False
    assert math.isfinite(stage["ms"]) and stage["ms"] >= 0
    assert "answer" not in stage


def test_chat_trace_marks_business_refusal(client: TestClient) -> None:
    client.app.dependency_overrides[get_chat_client] = lambda: FakeChat()

    response = client.post(
        "/v1/chat/stream",
        json={"kb_id": KB, "question": "内部资料", "audience": "internal"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    assert response.text.rstrip().endswith("data: [DONE]")
    stage = _get_trace(client, response.headers["X-Trace-Id"]).json()["stages"][0]
    assert stage["stage"] == "answer"
    assert stage["mode"] == "chat"
    assert stage["hits"] == 0
    assert stage["refused"] is True


def test_chat_failure_is_not_counted_as_business_refusal(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("REFUSAL_MIN_SIMILARITY", "0")
    client.app.dependency_overrides[get_chat_client] = lambda: FailingChat()

    response = client.post(
        "/v1/chat/stream",
        json={"kb_id": KB, "question": "退款多久到账"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    assert "event: error" in response.text
    stage = _get_trace(client, response.headers["X-Trace-Id"]).json()["stages"][0]
    assert stage["stage"] == "error"
    assert stage["mode"] == "error"
    assert stage["hits"] == 0
    assert stage["refused"] is False


def test_trace_write_failure_does_not_break_search(client: TestClient, tmp_path) -> None:
    from app.deps import get_trace_store
    from src.trace import TraceStore

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    store = TraceStore(blocked / "traces")
    client.app.dependency_overrides[get_trace_store] = lambda: store

    response = client.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    trace_response = _get_trace(client, response.headers["X-Trace-Id"])
    assert trace_response.status_code == 200
    assert trace_response.json()["stages"][0]["query"] == "退款"
