"""调测驾驶舱摘要：`GET /v1/ops/summary?kb_id=...`。

只读聚合，所有查询带当前租户；知识库必须属于当前租户，否则 404。
组件探测（parser）必须短超时且失败降级为 down，不能拖垮摘要接口。
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException

from app.deps import (
    EngineDep,
    TenantId,
    TraceStoreDep,
    UserId,
    ops_actions_enabled,
    require_admin,
)
from app.services import ensure_kb
from src import ingest_jobs, ops_audit, ops_runs
from src.ingest_events import list_job_events, list_workers
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


def _job_metrics(job: dict) -> dict:
    created = job.get("created_at")
    started = job.get("started_at")
    finished = job.get("finished_at")
    job["wait_seconds"] = (
        round((started - created).total_seconds(), 3) if created and started else None
    )
    job["duration_seconds"] = (
        round((finished - started).total_seconds(), 3)
        if started and finished
        else None
    )
    return job


@router.get("/ops/jobs")
def ops_jobs(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    all_jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=500)
    counts = {
        key: sum(1 for job in all_jobs if job["status"] == key)
        for key in ("queued", "running", "succeeded", "failed")
    }
    jobs = all_jobs
    if status:
        jobs = [job for job in jobs if job["status"] == status]
    jobs = [_job_metrics(job) for job in jobs[: max(1, min(limit, 200))]]
    return {"jobs": jobs, "counts": counts}


@router.get("/ops/jobs/{job_id}/events")
def ops_job_events(
    job_id: int,
    tenant_id: TenantId,
    engine: EngineDep,
) -> dict:
    job = ingest_jobs.get_job(engine, tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "events": list_job_events(engine, tenant_id, job_id)}


@router.get("/ops/traces")
def ops_traces(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    trace_store: TraceStoreDep,
    limit: int = 20,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    return {
        "traces": trace_store.list_recent(
            tenant_id, kb_id, limit=max(1, min(limit, 200))
        )
    }


def _require_actions(
    engine, tenant_id: str, user_id: str, action: str, target: str | None
) -> None:
    """运维动作准入：必须管理员且开关开启，拒绝时落一条 denied 审计。"""
    try:
        require_admin(engine, tenant_id, user_id)
        if not ops_actions_enabled():
            raise HTTPException(status_code=403, detail="ops actions disabled")
    except HTTPException as exc:
        ops_audit.record(
            engine, tenant_id=tenant_id, actor=user_id, action=action,
            target=target, result="denied", reason=str(exc.detail),
        )
        raise


@router.get("/ops/runs")
def list_runs_api(kb_id: str, tenant_id: TenantId, engine: EngineDep) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    return {"runs": ops_runs.list_runs(engine, tenant_id, kb_id)}


@router.get("/ops/runs/{run_id}")
def get_run_api(run_id: int, tenant_id: TenantId, engine: EngineDep) -> dict:
    run = ops_runs.get_run(engine, tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/ops/audit")
def audit_api(
    tenant_id: TenantId, user_id: UserId, engine: EngineDep, limit: int = 50
) -> dict:
    _require_actions(engine, tenant_id, user_id, "audit", None)
    return {"audit": ops_audit.list_records(engine, tenant_id, limit=limit)}
