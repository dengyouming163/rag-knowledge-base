"""入库任务查询接口。

两个只读端点，租户一律来自依赖注入：
- `GET /v1/jobs/{job_id}`：按租户查单条任务，查不到就 404（不区分"不存在"和"不是你的"）；
- `GET /v1/jobs?kb_id=...`：返回该租户该知识库最近 20 条任务，按 id 倒序。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.deps import EngineDep, TenantId
from app.schemas import JobListResponse, JobOut
from src import ingest_jobs

router = APIRouter(prefix="/v1", tags=["jobs"])

LIST_LIMIT = 20


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: int, tenant_id: TenantId, engine: EngineDep) -> dict:
    job = ingest_jobs.get_job(engine, tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/jobs", response_model=JobListResponse)
def list_jobs(kb_id: str, tenant_id: TenantId, engine: EngineDep) -> JobListResponse:
    return JobListResponse(
        jobs=ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=LIST_LIMIT)
    )
