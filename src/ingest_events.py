"""任务阶段事件与 worker 心跳：best-effort 写入，不反向影响入库。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def record_job_event(
    engine: Engine,
    *,
    job_id: int,
    tenant_id: str,
    stage: str,
    status: str,
    progress: int | None = None,
    message: str = "",
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ingest_job_events "
                    "(job_id, tenant_id, stage, status, progress, message) "
                    "VALUES (:job_id, :tenant_id, :stage, :status, :progress, :message)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "stage": stage,
                    "status": status,
                    "progress": progress,
                    "message": message[:1000],
                },
            )
    except Exception:  # noqa: BLE001 - 事件失败不能影响入库
        logger.exception("任务事件写入失败：job_id=%s stage=%s", job_id, stage)


def list_job_events(engine: Engine, tenant_id: str, job_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, job_id, tenant_id, stage, status, progress, message, created_at "
                "FROM ingest_job_events "
                "WHERE tenant_id = :tenant_id AND job_id = :job_id ORDER BY id"
            ),
            {"tenant_id": tenant_id, "job_id": job_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def record_worker_heartbeat(
    engine: Engine,
    *,
    worker_id: str,
    version: str = "",
    current_job_id: int | None = None,
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ops_workers "
                    "(worker_id, last_seen, version, current_job_id, updated_at) "
                    "VALUES (:worker_id, :now, :version, :job_id, :now) "
                    "ON CONFLICT (worker_id) DO UPDATE SET "
                    "last_seen = EXCLUDED.last_seen, "
                    "version = EXCLUDED.version, "
                    "current_job_id = EXCLUDED.current_job_id, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "worker_id": worker_id,
                    "now": datetime.now(UTC),
                    "version": version,
                    "job_id": current_job_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("worker 心跳写入失败：worker_id=%s", worker_id)


def list_workers(engine: Engine, tenant_id: str) -> list[dict]:
    """worker 健康是全局的，但 current_job_id 只在属于本租户时返回。"""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT w.worker_id, w.last_seen, w.version, "
                "CASE WHEN j.tenant_id = :tenant_id THEN w.current_job_id ELSE NULL END "
                "AS current_job_id "
                "FROM ops_workers w "
                "LEFT JOIN ingest_jobs j ON j.id = w.current_job_id "
                "ORDER BY w.last_seen DESC"
            ),
            {"tenant_id": tenant_id},
        ).mappings().all()
    now = datetime.now(UTC)
    return [
        {
            **dict(row),
            "age_seconds": max(0.0, (now - row["last_seen"]).total_seconds()),
        }
        for row in rows
    ]
