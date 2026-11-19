"""运营报表：业务查询指标、知识缺口与入库任务聚合。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.deps import get_trace_store
from app.main import create_app
from src.db.base import create_db_engine
from src.ingest_jobs import enqueue, mark_failed, mark_succeeded
from src.trace import TraceStore

TENANT = "t_api_stats"
OTHER_TENANT = "t_api_stats_other"
KB = "kb_api_stats"


def _trace(
    stage: str,
    query: str,
    *,
    hits: int = 0,
    refused: bool = False,
) -> dict:
    return {
        "trace_id": f"trace-{stage}-{query}",
        "tenant": "t_stats",
        "kb": "kb_stats",
        "stage": stage,
        "ms": 1.0,
        "hits": hits,
        "refused": refused,
        "mode": "chat" if stage == "answer" else "hybrid",
        "query": query,
        "ts": "2026-11-19T12:00:00.000Z",
        "seq": 0,
    }


def test_build_stats_uses_business_requests_and_normalized_query_rankings() -> None:
    from src.stats import build_stats

    traces = [
        _trace("retrieve", " 无理由   退款 "),
        _trace("retrieve", "无理由 退款"),
        _trace("retrieve", "Beta"),
        _trace("retrieve", "beta"),
        _trace("retrieve", "Alpha"),
        _trace("retrieve", "alpha"),
        _trace("retrieve", "正常命中", hits=3),
        _trace("answer", "无理由   退款", refused=True),
        _trace("answer", "账户 锁定", hits=1, refused=True),
        _trace("answer", "已解决", hits=2),
        _trace("error", "无理由 退款"),
        _trace("cancelled", "系统取消"),
    ]
    jobs = [
        {"status": "queued"},
        {"status": "running"},
        {"status": "succeeded"},
        {"status": "succeeded"},
        {"status": "failed"},
    ]

    result = build_stats("kb_stats", traces, jobs, top_n=4)

    assert result == {
        "kb_id": "kb_stats",
        "total_requests": 10,
        "coverage": 0.3,
        "refusal_rate": 0.6667,
        "zero_result_top": [
            {"query": "无理由 退款", "count": 3},
            {"query": "alpha", "count": 2},
            {"query": "beta", "count": 2},
        ],
        "gap_top": [
            {"query": "无理由 退款", "count": 3, "zero_results": 3, "refusals": 1},
            {"query": "alpha", "count": 2, "zero_results": 2, "refusals": 0},
            {"query": "beta", "count": 2, "zero_results": 2, "refusals": 0},
            {"query": "账户 锁定", "count": 1, "zero_results": 0, "refusals": 1},
        ],
        "ingest": {
            "total": 5,
            "queued": 1,
            "running": 1,
            "succeeded": 2,
            "failed": 1,
            "success_rate": 0.6667,
        },
    }


def test_build_stats_empty_data_has_zero_ratios_and_empty_rankings() -> None:
    from src.stats import build_stats

    assert build_stats("kb_empty", [], []) == {
        "kb_id": "kb_empty",
        "total_requests": 0,
        "coverage": 0.0,
        "refusal_rate": 0.0,
        "zero_result_top": [],
        "gap_top": [],
        "ingest": {
            "total": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "success_rate": 0.0,
        },
    }


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过报表接口集成测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'api stats')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'stats kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()


@pytest.fixture()
def api(engine, tmp_path):
    store = TraceStore(tmp_path / "api-traces")
    app = create_app()
    app.dependency_overrides[get_trace_store] = lambda: store
    with TestClient(app) as client:
        yield client, store


def _record(
    store: TraceStore,
    trace_id: str,
    stage: str,
    query: str,
    *,
    hits: int = 0,
    refused: bool = False,
    tenant: str = TENANT,
    kb: str = KB,
) -> dict:
    return store.record(
        trace_id=trace_id,
        tenant=tenant,
        kb=kb,
        stage=stage,
        ms=1.0,
        hits=hits,
        refused=refused,
        mode="chat" if stage == "answer" else "hybrid",
        query=query,
    )


def test_trace_store_lists_all_dates_and_unpersisted_memory_without_duplicates(tmp_path) -> None:
    old = _trace("retrieve", "旧记录")
    other_tenant = {**old, "tenant": "other", "trace_id": "other-tenant"}
    (tmp_path / "2020-01-01.jsonl").write_text(
        json.dumps(old, ensure_ascii=False) + "\n" + json.dumps(other_tenant) + "\n",
        encoding="utf-8",
    )
    store = TraceStore(tmp_path)
    memory_only = _record(store, "memory-only", "retrieve", "内存未落盘", tenant="t_stats", kb="kb_stats")
    current_path = tmp_path / f"{memory_only['ts'][:10]}.jsonl"
    current_path.unlink()
    persisted = _record(store, "persisted", "retrieve", "当天记录", tenant="t_stats", kb="kb_stats")
    _record(store, "other-kb", "retrieve", "其他库", tenant="t_stats", kb="other_kb")
    _record(store, "other-memory", "retrieve", "其他租户", tenant="other", kb="kb_stats")

    records = store.list_records("t_stats", "kb_stats")

    assert [item["query"] for item in records] == ["旧记录", "内存未落盘", "当天记录"]
    assert sum(item["trace_id"] == persisted["trace_id"] for item in records) == 1


def test_stats_api_aggregates_all_jobs_and_file_and_memory_traces(api, engine, tmp_path) -> None:
    client, store = api
    job_ids = [enqueue(engine, TENANT, KB, doc_id=f"doc_stats_{index}") for index in range(22)]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ingest_jobs SET status = 'running' WHERE id = :id"),
            {"id": job_ids[0]},
        )
    mark_succeeded(engine, job_ids[1])
    mark_failed(engine, job_ids[2], "terminal", retry=False)

    _record(store, "zero-1", "retrieve", " 无理由 退款 ")
    _record(store, "zero-2", "retrieve", "无理由   退款")
    _record(store, "covered-search", "retrieve", "正常", hits=2)
    _record(store, "refused-chat", "answer", "账户 锁定", refused=True)
    _record(store, "answered-chat", "answer", "已解决", hits=1)
    _record(store, "system-error", "error", "无理由 退款")
    _record(store, "system-cancelled", "cancelled", "取消")
    memory_only = _record(store, "memory-only", "retrieve", "内存未落盘")
    trace_path = tmp_path / "api-traces" / f"{memory_only['ts'][:10]}.jsonl"
    persisted_lines = [
        line
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["trace_id"] != "memory-only"
    ]
    trace_path.write_text("\n".join(persisted_lines) + "\n", encoding="utf-8")

    response = client.get(
        "/v1/stats", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    )

    assert response.status_code == 200
    assert response.json() == {
        "kb_id": KB,
        "total_requests": 6,
        "coverage": 0.3333,
        "refusal_rate": 0.5,
        "zero_result_top": [
            {"query": "无理由 退款", "count": 2},
            {"query": "内存未落盘", "count": 1},
            {"query": "账户 锁定", "count": 1},
        ],
        "gap_top": [
            {"query": "无理由 退款", "count": 2, "zero_results": 2, "refusals": 0},
            {"query": "内存未落盘", "count": 1, "zero_results": 1, "refusals": 0},
            {"query": "账户 锁定", "count": 1, "zero_results": 1, "refusals": 1},
        ],
        "ingest": {
            "total": 22,
            "queued": 19,
            "running": 1,
            "succeeded": 1,
            "failed": 1,
            "success_rate": 0.5,
        },
    }

    limited = client.get(
        "/v1/stats", params={"kb_id": KB, "top_n": 1}, headers={"X-Tenant-Id": TENANT}
    )
    assert limited.status_code == 200
    assert limited.json()["zero_result_top"] == [{"query": "无理由 退款", "count": 2}]
    assert len(limited.json()["gap_top"]) == 1


def test_stats_api_validates_top_n_and_hides_other_tenant_kb(api) -> None:
    client, _ = api
    headers = {"X-Tenant-Id": TENANT}

    for top_n in (0, 51):
        response = client.get("/v1/stats", params={"kb_id": KB, "top_n": top_n}, headers=headers)
        assert response.status_code == 422

    response = client.get(
        "/v1/stats", params={"kb_id": KB}, headers={"X-Tenant-Id": OTHER_TENANT}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "knowledge base not found"}
