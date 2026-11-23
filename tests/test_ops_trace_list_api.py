"""指标 trace 的游标列表、详情合并与旧 traces 接口兼容性。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.deps import get_trace_store
from app.main import create_app
from src.db.base import create_db_engine
from src.ops_observability import record_rag_metric
from src.trace import TraceStore

TENANT = "t_ops_trace_list"
OTHER_TENANT = "t_ops_trace_list_other"
KB = "kb_ops_trace_list"
SECOND_KB = "kb_ops_trace_list_second"
OTHER_KB = "kb_ops_trace_list_other"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过 trace 列表接口测试：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        for tenant in (TENANT, OTHER_TENANT):
            connection.execute(
                text("INSERT INTO tenants (id, name) VALUES (:t, 'trace list')"),
                {"t": tenant},
            )
        for kb, tenant in (
            (KB, TENANT),
            (SECOND_KB, TENANT),
            (OTHER_KB, OTHER_TENANT),
        ):
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases "
                    "(id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, 'trace list', '', 'active')"
                ),
                {"kb": kb, "t": tenant},
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
            text("DELETE FROM knowledge_bases WHERE tenant_id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:t, :o)"),
            {"t": TENANT, "o": OTHER_TENANT},
        )


@pytest.fixture()
def trace_store(tmp_path) -> TraceStore:
    return TraceStore(tmp_path)


@pytest.fixture()
def client(engine, trace_store) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_trace_store] = lambda: trace_store
    yield TestClient(app)
    app.dependency_overrides.clear()


def _get(client: TestClient, path: str, *, tenant: str = TENANT, **params):
    return client.get(
        path,
        params={"kb_id": KB, **params},
        headers={"X-Tenant-Id": tenant},
    )


def _rag(
    engine,
    trace_id: str,
    *,
    tenant: str = TENANT,
    kb: str = KB,
    occurred_at: datetime | None = None,
    **overrides,
) -> None:
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
    if occurred_at is not None:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE ops_rag_metrics SET occurred_at=:occurred_at "
                    "WHERE tenant_id=:tenant AND trace_id=:trace_id"
                ),
                {
                    "occurred_at": occurred_at,
                    "tenant": tenant,
                    "trace_id": trace_id,
                },
            )


def _event(store: TraceStore, trace_id: str, *, tenant=TENANT, kb=KB, stage="retrieve"):
    return store.record(
        trace_id=trace_id,
        tenant=tenant,
        kb=kb,
        stage=stage,
        ms=1.0,
        hits=2,
        refused=False,
        mode="hybrid",
        query=f"query-{stage}",
    )


def test_trace_list_paginates_three_pages_with_stable_tie_break(
    client: TestClient, engine
) -> None:
    base = datetime.now(UTC) - timedelta(minutes=5)
    for trace_id, occurred_at in (
        ("page-oldest", base - timedelta(minutes=2)),
        ("page-tie-a", base - timedelta(minutes=1)),
        ("page-tie-b", base - timedelta(minutes=1)),
        ("page-tie-c", base - timedelta(minutes=1)),
        ("page-newest", base),
    ):
        _rag(engine, trace_id, occurred_at=occurred_at)

    pages = []
    cursors = []
    cursor = None
    for _ in range(3):
        params = {"limit": 2, **(cursor or {})}
        response = _get(client, "/v1/ops/trace-list", **params)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"items", "next_cursor"}
        pages.append(body["items"])
        cursor = body["next_cursor"]
        cursors.append(cursor)
        if cursor is not None:
            last = body["items"][-1]
            assert cursor == {
                "before_occurred_at": last["occurred_at"],
                "before_id": last["id"],
            }
            parsed = datetime.fromisoformat(
                cursor["before_occurred_at"].replace("Z", "+00:00")
            )
            assert parsed.tzinfo is not None

    assert [[item["trace_id"] for item in page] for page in pages] == [
        ["page-newest", "page-tie-c"],
        ["page-tie-b", "page-tie-a"],
        ["page-oldest"],
    ]
    assert [cursor is not None for cursor in cursors] == [True, True, False]
    tie_ids = [item["id"] for item in pages[0][1:] + pages[1]]
    assert tie_ids == sorted(tie_ids, reverse=True)

    empty = _get(
        client,
        "/v1/ops/trace-list",
        limit=2,
        before_occurred_at=pages[-1][-1]["occurred_at"],
        before_id=pages[-1][-1]["id"],
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_cursor": None}


def test_trace_list_validates_cursor_window_and_limit(client: TestClient, engine) -> None:
    now = datetime.now(UTC)
    for index in range(21):
        _rag(engine, f"limit-{index}", occurred_at=now - timedelta(seconds=index))

    default = _get(client, "/v1/ops/trace-list")
    assert default.status_code == 200
    assert len(default.json()["items"]) == 20
    assert default.json()["next_cursor"] is not None
    assert len(_get(client, "/v1/ops/trace-list", limit=1).json()["items"]) == 1
    last_page = _get(client, "/v1/ops/trace-list", limit=100)
    assert len(last_page.json()["items"]) == 21
    assert last_page.json()["next_cursor"] is None

    occurred_at = default.json()["items"][0]["occurred_at"]
    invalid_params = [
        {"limit": 0},
        {"limit": 101},
        {"window": "1m"},
        {"min_ms": -1},
        {"before_occurred_at": occurred_at},
        {"before_id": default.json()["items"][0]["id"]},
        {"before_occurred_at": "not-a-date", "before_id": 1},
    ]
    for params in invalid_params:
        assert _get(client, "/v1/ops/trace-list", **params).status_code == 422


def test_trace_list_applies_all_filters_and_exact_trace_id(
    client: TestClient, engine
) -> None:
    now = datetime.now(UTC)
    common = {
        "operation": "search",
        "mode": "vector",
        "outcome": "refused",
        "total_ms": 150.0,
    }
    rows = [
        ("trace-target", now - timedelta(minutes=5), {"total_ms": 100.0}),
        ("trace-other-match", now - timedelta(minutes=4), {}),
        ("trace-wrong-outcome", now - timedelta(minutes=3), {"outcome": "succeeded"}),
        ("trace-wrong-operation", now - timedelta(minutes=3), {"operation": "chat"}),
        ("trace-wrong-mode", now - timedelta(minutes=3), {"mode": "hybrid"}),
        ("trace-too-fast", now - timedelta(minutes=3), {"total_ms": 99.0}),
        ("trace-old", now - timedelta(hours=25), {}),
        ("trace-future", now + timedelta(hours=1), {}),
    ]
    for trace_id, occurred_at, overrides in rows:
        _rag(
            engine,
            trace_id,
            occurred_at=occurred_at,
            **{**common, **overrides},
        )

    filters = {
        "outcome": "refused",
        "operation": "search",
        "mode": "vector",
        "min_ms": 100,
    }
    response = _get(client, "/v1/ops/trace-list", **filters)
    assert response.status_code == 200
    assert {item["trace_id"] for item in response.json()["items"]} == {
        "trace-target",
        "trace-other-match",
    }

    exact = _get(client, "/v1/ops/trace-list", trace_id="trace-target", **filters)
    assert [item["trace_id"] for item in exact.json()["items"]] == ["trace-target"]
    fuzzy = _get(client, "/v1/ops/trace-list", trace_id="trace-", **filters)
    assert fuzzy.json() == {"items": [], "next_cursor": None}

    wider = _get(client, "/v1/ops/trace-list", window="7d", **filters)
    assert {item["trace_id"] for item in wider.json()["items"]} == {
        "trace-target",
        "trace-other-match",
        "trace-old",
    }


def test_trace_detail_merges_real_metric_spans_and_original_events(
    client: TestClient, engine, trace_store: TraceStore
) -> None:
    span = {"stage": "retrieve", "start_ms": 1.0, "duration_ms": 12.5, "status": "ok"}
    _rag(
        engine,
        "detail-full",
        operation="search",
        mode="vector",
        total_ms=42.0,
        stage_spans=[span],
        retrieval_ms=12.5,
        error_kind=None,
    )
    _event(trace_store, "detail-full", stage="retrieve")
    _event(trace_store, "detail-full", stage="answer")
    _event(trace_store, "detail-full", kb=SECOND_KB, stage="other-kb")
    _event(trace_store, "detail-full", tenant=OTHER_TENANT, kb=OTHER_KB, stage="other-tenant")

    response = _get(client, "/v1/ops/traces/detail-full/detail")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"metric", "events", "missing"}
    assert body["metric"]["trace_id"] == "detail-full"
    assert body["metric"]["stage_spans"] == [span]
    assert body["metric"]["total_ms"] == 42.0
    assert body["metric"]["retrieval_ms"] == 12.5
    assert body["missing"] == []
    assert [event["stage"] for event in body["events"]] == ["retrieve", "answer"]
    assert [event["query"] for event in body["events"]] == [
        "query-retrieve",
        "query-answer",
    ]


def test_trace_detail_reports_each_missing_source_and_404_for_none(
    client: TestClient, engine, trace_store: TraceStore
) -> None:
    _rag(engine, "detail-metric-only")
    metric_only = _get(client, "/v1/ops/traces/detail-metric-only/detail")
    assert metric_only.status_code == 200
    assert metric_only.json()["metric"]["trace_id"] == "detail-metric-only"
    assert metric_only.json()["events"] == []
    assert metric_only.json()["missing"] == ["events"]

    _event(trace_store, "detail-events-only")
    events_only = _get(client, "/v1/ops/traces/detail-events-only/detail")
    assert events_only.status_code == 200
    assert events_only.json()["metric"] is None
    assert [event["trace_id"] for event in events_only.json()["events"]] == [
        "detail-events-only"
    ]
    assert events_only.json()["missing"] == ["metric"]

    assert _get(client, "/v1/ops/traces/detail-absent/detail").status_code == 404


def test_trace_list_and_detail_do_not_leak_across_tenant_or_kb(
    client: TestClient, engine, trace_store: TraceStore
) -> None:
    _rag(engine, "kb-secret", kb=SECOND_KB)
    _rag(engine, "tenant-secret", tenant=OTHER_TENANT, kb=OTHER_KB)
    _event(trace_store, "kb-secret", kb=SECOND_KB)
    _event(trace_store, "tenant-secret", tenant=OTHER_TENANT, kb=OTHER_KB)

    secret_list = _get(client, "/v1/ops/trace-list", trace_id="kb-secret")
    absent_list = _get(client, "/v1/ops/trace-list", trace_id="absent")
    assert secret_list.status_code == absent_list.status_code == 200
    assert secret_list.json() == absent_list.json() == {"items": [], "next_cursor": None}
    secret_detail = _get(client, "/v1/ops/traces/kb-secret/detail")
    absent_detail = _get(client, "/v1/ops/traces/absent/detail")
    assert secret_detail.status_code == absent_detail.status_code == 404
    assert secret_detail.json() == absent_detail.json()

    for tenant, kb in ((OTHER_TENANT, KB), (TENANT, OTHER_KB)):
        assert _get(
            client, "/v1/ops/trace-list", tenant=tenant, kb_id=kb
        ).status_code == 404
        assert _get(
            client, "/v1/ops/traces/tenant-secret/detail", tenant=tenant, kb_id=kb
        ).status_code == 404


def test_legacy_traces_response_is_unchanged(
    client: TestClient, trace_store: TraceStore
) -> None:
    _event(trace_store, "legacy-trace")

    response = _get(client, "/v1/ops/traces")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"traces"}
    assert body["traces"][0]["trace_id"] == "legacy-trace"
    assert body["traces"][0]["query"] == "query-retrieve"
