"""调测驾驶舱摘要：`GET /v1/ops/summary?kb_id=...`。

只读聚合，所有查询带当前租户；知识库必须属于当前租户，否则 404。
组件探测（parser）必须短超时且失败降级为 down，不能拖垮摘要接口。
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter

from app.deps import EngineDep, TenantId, TraceStoreDep
from app.services import ensure_kb
from src import ingest_jobs, ops_runs
from src.ingest_events import list_workers
from src.ops_metrics import build_ops_summary

router = APIRouter(prefix="/v1", tags=["ops"])


def _parser_health() -> str:
    base_url = os.environ.get("PARSER_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return "unconfigured"
    try:
        response = httpx.get(f"{base_url}/health", timeout=0.8)
        return "up" if response.status_code < 500 else "down"
    except Exception:  # noqa: BLE001 - 探测失败就是 down，不影响摘要
        return "down"


@router.get("/ops/summary")
def ops_summary(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    trace_store: TraceStoreDep,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=500)
    traces = trace_store.list_records(tenant_id, kb_id)
    workers = list_workers(engine)
    health = {
        "api": "up",
        "db": "up",
        "parser": _parser_health(),
        "workers": workers,
    }
    return build_ops_summary(
        health=health,
        jobs=jobs,
        traces=traces,
        runs=ops_runs.list_runs(engine, tenant_id, kb_id, limit=5),
        workers=workers,
    )
