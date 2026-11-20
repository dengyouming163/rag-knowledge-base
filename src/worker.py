"""后台入库 worker：领取任务、执行入库，并把终态同步给任务与文档。

每轮最多处理一条任务。单条文档的解析、读取或嵌入异常只负责让该任务进入
重试/失败流程，不能拖垮整个 worker 进程，否则后续任务会永远得不到处理。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .ingest import NonRetryableIngestError, ingest_document
from .ingest_events import record_job_event, record_worker_heartbeat
from .ingest_jobs import (
    claim_next,
    get_job,
    mark_failed,
    mark_progress,
    mark_succeeded,
)

UPLOAD_ROOT = Path("data/uploads")


def worker_once(
    engine: Engine,
    *,
    embed_client: Any,
    parser: Any = None,
    worker_id: str | None = None,
) -> int:
    """跑一轮：领到任务就执行并返回 1（无论成败）；没领到返回 0。"""
    worker_id = worker_id or os.environ.get("WORKER_ID", "worker-local")
    job = claim_next(engine)
    record_worker_heartbeat(
        engine,
        worker_id=worker_id,
        current_job_id=job["id"] if job else None,
    )
    if job is None:
        return 0

    job_id = job["id"]

    def on_stage(stage: str, progress: int, message: str = "") -> None:
        mark_progress(engine, job_id, progress)
        record_worker_heartbeat(
            engine, worker_id=worker_id, current_job_id=job_id
        )
        record_job_event(
            engine,
            job_id=job_id,
            tenant_id=job["tenant_id"],
            stage=stage,
            status="started",
            progress=progress,
            message=message,
        )

    record_job_event(
        engine,
        job_id=job_id,
        tenant_id=job["tenant_id"],
        stage="claimed",
        status="started",
        progress=10,
    )

    try:
        with engine.begin() as connection:
            source_url = connection.execute(
                text(
                    "SELECT source_url FROM documents "
                    "WHERE id = :doc_id AND tenant_id = :tenant_id AND kb_id = :kb_id"
                ),
                {
                    "doc_id": job["doc_id"],
                    "tenant_id": job["tenant_id"],
                    "kb_id": job["kb_id"],
                },
            ).scalar_one_or_none()
        if not source_url:
            raise ValueError(
                f"文档 {job['doc_id']} 不存在或 source_url 为空，无法定位上传文件"
            )

        path = UPLOAD_ROOT / source_url
        result = ingest_document(
            engine,
            tenant_id=job["tenant_id"],
            kb_id=job["kb_id"],
            doc_id=job["doc_id"],
            path=path,
            embed_client=embed_client,
            parser=parser,
            on_stage=on_stage,
        )
        mark_succeeded(engine, job_id)
        record_job_event(
            engine,
            job_id=job_id,
            tenant_id=job["tenant_id"],
            stage="succeeded",
            status="succeeded",
            progress=100,
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        mark_failed(
            engine,
            job_id,
            error,
            retry=not isinstance(exc, NonRetryableIngestError),
        )
        record_job_event(
            engine,
            job_id=job_id,
            tenant_id=job["tenant_id"],
            stage="failed",
            status="failed",
            progress=0,
            message=error,
        )
        failed_job = get_job(engine, job["tenant_id"], job_id)
        if failed_job is not None and failed_job["status"] == "failed":
            # 重试期间文档保持 pending；只有任务终态失败时才同步文档状态，
            # 避免临时故障让用户误以为文件已不可恢复。
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE documents SET status = 'failed', updated_at = now() "
                        "WHERE id = :doc_id AND tenant_id = :tenant_id"
                    ),
                    {"doc_id": job["doc_id"], "tenant_id": job["tenant_id"]},
                )
        print(
            f"worker 入库失败：job_id={job_id} doc_id={job['doc_id']} error={error}",
            file=sys.stderr,
        )
    else:
        print(
            f"worker 入库成功：job_id={job_id} doc_id={job['doc_id']} "
            f"chunks={result['chunks']}"
        )
    finally:
        record_worker_heartbeat(engine, worker_id=worker_id, current_job_id=None)

    return 1
