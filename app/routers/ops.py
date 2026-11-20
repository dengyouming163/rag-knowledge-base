"""调测驾驶舱摘要：`GET /v1/ops/summary?kb_id=...`。

只读聚合，所有查询带当前租户；知识库必须属于当前租户，否则 404。
组件探测（parser）必须短超时且失败降级为 down，不能拖垮摘要接口。
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

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
from src.ingest_events import list_job_events, list_workers, record_job_event
from src.ops_metrics import build_ops_summary
from src.ops_observability_report import (
    list_rag_metric_rows,
    resolve_window,
    summarize_observability,
)
from src.ops_pricing import load_pricing_from_env

router = APIRouter(prefix="/v1", tags=["ops"])

logger = logging.getLogger(__name__)


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
    window: str = "24h",
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    try:
        resolve_window(window)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        pricing = load_pricing_from_env()
    except ValueError:
        logger.exception("invalid model pricing configuration")
        pricing = {}

    observability = summarize_observability(
        engine, tenant_id, kb_id, window, pricing
    )
    latest_traces = list_rag_metric_rows(engine, tenant_id, kb_id, limit=8)
    jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=500)
    workers = list_workers(engine, tenant_id)
    health = {
        "api": "up",
        "db": "up",
        "parser": _parser_health(),
        "workers": workers,
    }
    return build_ops_summary(
        health=health,
        jobs=jobs,
        observability=observability,
        latest_traces=latest_traces,
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


def _safe_audit(engine, **kwargs) -> None:
    """审计是旁路：写入失败只记日志，不把已成功的动作变成 500。"""
    try:
        ops_audit.record(engine, **kwargs)
    except Exception:  # 审计失败不能掩盖动作结果
        logger.exception("ops audit write failed: %s", kwargs.get("action"))


def _require_actions(
    engine, tenant_id: str, user_id: str, action: str, target: str | None
) -> None:
    """写动作准入：必须管理员且开关开启，拒绝时落一条 denied 审计。

    仅用于写动作（POST/retry）；读接口不要用，避免被拒的读请求污染审计流水。
    """
    try:
        require_admin(engine, tenant_id, user_id)
        if not ops_actions_enabled():
            raise HTTPException(status_code=403, detail="ops actions disabled")
    except HTTPException as exc:
        _safe_audit(
            engine, tenant_id=tenant_id, actor=user_id, action=action,
            target=target, result="denied", reason=str(exc.detail),
        )
        raise


class RunRequest(BaseModel):
    kind: str = Field(pattern="^(eval_mock|loadtest|drill_parser|drill_llm|drill_queue)$")
    params: dict[str, str | int | float | bool] = Field(
        default_factory=dict, max_length=20
    )

    @field_validator("params")
    @classmethod
    def _limit_params_size(cls, value: dict) -> dict:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except ValueError as exc:
            # 非有限浮点无法写入 JSONB；且 Starlette 渲染 422 时同样会因为
            # 校验错误里的 input 含 Infinity 而失败，故在这里直接拒绝。
            raise HTTPException(
                status_code=422, detail="params must contain finite numbers"
            ) from exc
        if len(encoded) > 4096:
            raise ValueError("params too large: max 4096 characters")
        return value


@router.post("/ops/kbs/{kb_id}/runs", status_code=202)
def enqueue_run(
    kb_id: str,
    payload: RunRequest,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    _require_actions(engine, tenant_id, user_id, f"run_{payload.kind}", kb_id)
    try:
        run_id = ops_runs.enqueue(
            engine, tenant_id, kb_id, payload.kind,
            requested_by=user_id, params=payload.params,
        )
    except ops_runs.AlreadyRunning as exc:
        _safe_audit(
            engine, tenant_id=tenant_id, actor=user_id,
            action=f"run_{payload.kind}", target=str(exc.run_id),
            result="denied", reason="already_running",
        )
        raise HTTPException(
            status_code=409,
            detail={"message": "run already active", "run_id": exc.run_id},
        ) from exc
    _safe_audit(
        engine, tenant_id=tenant_id, actor=user_id,
        action=f"run_{payload.kind}", target=str(run_id),
        result="allowed",
        detail={
            "params_keys": sorted(payload.params),
            "params_size": len(json.dumps(payload.params, ensure_ascii=False)),
        },
    )
    return {"run_id": run_id, "status": "queued"}


@router.post("/ops/jobs/{job_id}/retry", status_code=202)
def retry_job_api(
    job_id: int,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    _require_actions(engine, tenant_id, user_id, "job_retry", str(job_id))
    job = ingest_jobs.retry_job(engine, tenant_id, job_id)
    if job is None:
        existing = ingest_jobs.get_job(engine, tenant_id, job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="job not found")
        raise HTTPException(status_code=409, detail="job is not failed")
    record_job_event(
        engine, job_id=job_id, tenant_id=tenant_id,
        stage="queued", status="started", progress=0,
        message="manual retry",
    )
    _safe_audit(
        engine, tenant_id=tenant_id, actor=user_id,
        action="job_retry", target=str(job_id), result="allowed",
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/ops/runs")
def list_runs_api(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    kind: str | None = None,
    limit: int = 20,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    return {
        "runs": ops_runs.list_runs(
            engine, tenant_id, kb_id, kind=kind,
            limit=max(1, min(limit, 200)),
        )
    }


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
    # 读接口：拒绝不写审计，避免任意调用方刷写审计流水
    require_admin(engine, tenant_id, user_id)
    if not ops_actions_enabled():
        raise HTTPException(status_code=403, detail="ops actions disabled")
    return {"audit": ops_audit.list_records(engine, tenant_id, limit=limit)}
