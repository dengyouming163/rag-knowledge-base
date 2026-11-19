"""运营报表接口：`GET /v1/stats?kb_id=...`。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.deps import EngineDep, TenantId, TraceStoreDep
from app.services import ensure_kb
from src import ingest_jobs
from src.stats import build_stats

router = APIRouter(prefix="/v1", tags=["stats"])


@router.get("/stats")
def get_stats(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    trace_store: TraceStoreDep,
    top_n: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=None)
    traces = trace_store.list_records(tenant_id, kb_id)
    return build_stats(kb_id, traces, jobs, top_n=top_n)
