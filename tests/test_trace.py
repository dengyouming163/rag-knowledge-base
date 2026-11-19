"""请求 trace：阶段耗时、JSONL 持久化、租户隔离与 SSE 回查。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest
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


def test_find_merges_file_when_first_stage_leaves_1000_record_ring(tmp_path) -> None:
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    _record(store, "abc123def456", stage="retrieve", query="original")
    for index in range(1000):
        _record(store, f"{index:012d}")
    _record(store, "abc123def456", stage="answer", query="original")

    assert all(
        not (item["trace_id"] == "abc123def456" and item["stage"] == "retrieve")
        for item in store._records
    )
    stages = store.find("abc123def456", TENANT)
    assert [item["stage"] for item in stages] == ["retrieve", "answer"]
    assert len(stages) == 2


def test_find_preserves_record_order_when_first_append_fails_with_same_ts(
    tmp_path, monkeypatch
) -> None:
    from src import trace as trace_module

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 19, 12, 0, 0, 123000, tzinfo=tz)

    monkeypatch.setattr(trace_module, "datetime", FrozenDateTime)
    original_open = Path.open
    append_attempts = 0

    def fail_first_append(self, mode="r", *args, **kwargs):
        nonlocal append_attempts
        if mode == "a":
            append_attempts += 1
            if append_attempts == 1:
                raise OSError("first append failed")
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_first_append)
    store = trace_module.TraceStore(tmp_path)
    answer = _record(store, "abc123def456", stage="answer")
    cancelled = _record(store, "abc123def456", stage="cancelled")

    assert answer["ts"] == cancelled["ts"]
    path = tmp_path / "2026-11-19.jsonl"
    persisted = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [item["stage"] for item in persisted] == ["cancelled"]
    assert [item["stage"] for item in store._records] == ["answer", "cancelled"]
    stages = store.find("abc123def456", TENANT)
    assert [item["stage"] for item in stages] == ["answer", "cancelled"]


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


def test_memory_append_does_not_wait_for_blocked_file_write(tmp_path, monkeypatch) -> None:
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original_open = Path.open

    def blocking_open(self, mode="r", *args, **kwargs):
        if mode == "a" and not started.is_set():
            started.set()
            release.wait(1)
        return original_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocking_open)
    first = threading.Thread(target=_record, args=(store, "first-record"))
    second = threading.Thread(target=_record, args=(store, "second-record"))
    first.start()
    assert started.wait(1)
    second.start()
    try:
        deadline = time.perf_counter() + 0.2
        while len(store._records) < 2 and time.perf_counter() < deadline:
            time.sleep(0.005)
        assert len(store._records) == 2
    finally:
        release.set()
        first.join(1)
        second.join(1)


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


def test_search_retrieve_failure_records_error_and_keeps_http_500_semantics(
    client: TestClient, monkeypatch
) -> None:
    from app.routers import search as search_module

    class ExplodingRetriever:
        def retrieve(self, *args, **kwargs):
            raise RuntimeError("retriever exploded")

    monkeypatch.setattr(
        search_module, "build_retriever", lambda *args, **kwargs: ExplodingRetriever()
    )
    http = TestClient(client.app, raise_server_exceptions=False)
    response = http.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款", "mode": "hybrid"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"].startswith("text/plain")
    trace_id = response.headers["X-Trace-Id"]
    trace_response = _get_trace(http, trace_id)
    assert trace_response.status_code == 200
    stages = trace_response.json()["stages"]
    assert len(stages) == 1
    stage = stages[0]
    assert stage["stage"] == "error"
    assert stage["mode"] == "hybrid"
    assert stage["query"] == "退款"
    assert stage["hits"] == 0
    assert stage["refused"] is False
    assert math.isfinite(stage["ms"]) and stage["ms"] >= 0
    assert "answer" not in stage


def test_search_retrieve_ms_includes_retriever_build(client: TestClient, monkeypatch) -> None:
    from app.routers import search as search_module

    clock = [0.0]

    class Retriever:
        def retrieve(self, *args, **kwargs):
            clock[0] += 0.005
            return []

    def slow_build(*args, **kwargs):
        clock[0] += 0.080
        return Retriever()

    monkeypatch.setattr(search_module, "build_retriever", slow_build)
    monkeypatch.setattr(search_module.time, "perf_counter", lambda: clock[0])
    response = client.post(
        f"/v1/kbs/{KB}/search",
        json={"query": "退款", "mode": "hybrid"},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 200
    stages = _get_trace(client, response.headers["X-Trace-Id"]).json()["stages"]
    assert len(stages) == 1
    assert stages[0]["stage"] == "retrieve"
    assert stages[0]["ms"] == pytest.approx(85.0)


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


@pytest.mark.parametrize("fails", [False, True])
def test_chat_record_does_not_block_event_loop(monkeypatch, fails: bool) -> None:
    from app.routers import chat as chat_module
    from app.schemas import ChatRequest

    class Pipeline:
        def answer(self, *args):
            if fails:
                raise RuntimeError("boom")
            return {"answer": "ok", "chunks": ["c1"], "rewritten": "question"}

    class BlockingStore:
        def __init__(self, release: threading.Event) -> None:
            self.release = release
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)
            self.release.wait(1)

    monkeypatch.setattr(chat_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: ([], False))
    monkeypatch.setattr(chat_module, "build_pipeline", lambda *args: Pipeline())

    async def exercise() -> None:
        release = threading.Event()
        store = BlockingStore(release)
        response = await chat_module.chat_stream(
            payload=ChatRequest(kb_id=KB, question="question"),
            tenant_id=TENANT,
            engine=object(),
            embed_client=object(),
            chat_client=object(),
            reranker=None,
            user_id="user",
            trace_store=store,
        )
        iterator = response.body_iterator
        timer = threading.Timer(0.2, release.set)
        timer.start()
        try:
            task = asyncio.create_task(anext(iterator))
            started = time.perf_counter()
            await asyncio.sleep(0.03)
            elapsed = time.perf_counter() - started
            frame = await asyncio.wait_for(task, 1)
            assert elapsed < 0.15
            assert ("event: error" in frame) if fails else ("event: hits" in frame)
            assert store.calls[0]["stage"] == ("error" if fails else "answer")
        finally:
            release.set()
            timer.join()
            await iterator.aclose()

    asyncio.run(exercise())


def test_chat_cancelled_while_waiting_answer_survives_repeated_cancel(monkeypatch) -> None:
    from app.routers import chat as chat_module
    from app.schemas import ChatRequest

    answer_started = threading.Event()
    answer_release = threading.Event()
    record_started = threading.Event()
    record_release = threading.Event()
    record_finished = threading.Event()

    class Pipeline:
        def answer(self, *args):
            answer_started.set()
            answer_release.wait(1)
            return {"answer": "ok", "chunks": ["c1"], "rewritten": "question"}

    class Store:
        def __init__(self) -> None:
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["stage"] == "cancelled":
                record_started.set()
                record_release.wait(1)
                record_finished.set()

    monkeypatch.setattr(chat_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: ([], False))
    monkeypatch.setattr(chat_module, "build_pipeline", lambda *args: Pipeline())

    async def exercise() -> None:
        store = Store()
        response = await chat_module.chat_stream(
            payload=ChatRequest(kb_id=KB, question="question"),
            tenant_id=TENANT,
            engine=object(),
            embed_client=object(),
            chat_client=object(),
            reranker=None,
            user_id="user",
            trace_store=store,
        )
        iterator = response.body_iterator

        async def wait_for(event: threading.Event) -> bool:
            deadline = asyncio.get_running_loop().time() + 1
            while not event.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            return event.is_set()

        task = asyncio.create_task(anext(iterator))
        try:
            assert await wait_for(answer_started)
            task.cancel()
            assert await wait_for(record_started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not record_finished.is_set()
            record_release.set()
            assert await wait_for(record_finished)
            assert [call["stage"] for call in store.calls] == ["cancelled"]
            cancelled = store.calls[0]
            assert cancelled["mode"] == "error"
            assert cancelled["hits"] == 0
            assert cancelled["refused"] is False
            assert cancelled["ms"] >= 0
            assert "answer" not in cancelled
        finally:
            answer_release.set()
            record_release.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    asyncio.run(exercise())


def test_chat_cancelled_during_sse_keeps_answer_and_cancelled_stages(monkeypatch) -> None:
    from app.routers import chat as chat_module
    from app.schemas import ChatRequest

    class Pipeline:
        def answer(self, *args):
            return {"answer": "ok", "chunks": ["c1"], "rewritten": "question"}

    class Store:
        def __init__(self) -> None:
            self.calls = []

        def record(self, **kwargs):
            self.calls.append(kwargs)

    monkeypatch.setattr(chat_module, "ensure_kb", lambda *args: None)
    monkeypatch.setattr(chat_module, "user_context_of", lambda *args: ([], False))
    monkeypatch.setattr(chat_module, "build_pipeline", lambda *args: Pipeline())

    async def exercise() -> None:
        store = Store()
        response = await chat_module.chat_stream(
            payload=ChatRequest(kb_id=KB, question="question"),
            tenant_id=TENANT,
            engine=object(),
            embed_client=object(),
            chat_client=object(),
            reranker=None,
            user_id="user",
            trace_store=store,
        )
        iterator = response.body_iterator
        assert "event: hits" in await anext(iterator)
        assert [call["stage"] for call in store.calls] == ["answer"]

        with pytest.raises(asyncio.CancelledError):
            await iterator.athrow(asyncio.CancelledError())
        assert [call["stage"] for call in store.calls] == ["answer", "cancelled"]
        assert store.calls[-1]["mode"] == "error"
        assert store.calls[-1]["refused"] is False
        assert "answer" not in store.calls[-1]

    asyncio.run(exercise())


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
