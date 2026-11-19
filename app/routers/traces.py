"""请求 trace 查询：`GET /v1/traces/{trace_id}`。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.deps import TenantId, TraceStoreDep

router = APIRouter(prefix="/v1", tags=["traces"])


@router.get("/traces/{trace_id}")
def get_trace(trace_id: str, tenant_id: TenantId, trace_store: TraceStoreDep):
    stages = trace_store.find(trace_id, tenant_id)
    if not stages:
        raise HTTPException(status_code=404, detail="trace not found")
    return {"trace_id": trace_id, "stages": stages}
