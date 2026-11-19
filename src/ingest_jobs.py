"""入库任务状态机：入队、领取、进度、成功、失败与重试。

状态流转：
    queued -> running -> succeeded
                      -> queued（未达重试上限）
                      -> failed（达到 MAX_ATTEMPTS）

约定：所有返回的 dict 由 `dict(row._mapping)` 构造，时间字段（created_at /
updated_at / finished_at）保留为带时区的 datetime 对象，不转 ISO 字符串。

ingest_jobs 未开启 RLS，普通事务即可；读取 chunks 等受 RLS 保护的表时应由调用方
使用 tenant_scope。本模块所有按 id 操作的任务都属于 worker 内部流程，
查询接口 `get_job` / `list_jobs` 必须携带 tenant_id 做租户隔离。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

STATUSES = ("queued", "running", "succeeded", "failed")
MAX_ATTEMPTS = 3

_JOB_COLUMNS = (
    "id, tenant_id, kb_id, doc_id, status, error, source, attempts, progress, "
    "finished_at, created_at, updated_at"
)


def enqueue(
    engine: Engine, tenant_id: str, kb_id: str, doc_id: str, *, source: str = "upload"
) -> int:
    """入队一条任务；同一文档已有 queued/running 任务时复用该任务 id。"""
    with engine.begin() as connection:
        existing = connection.execute(
            text(
                "SELECT id FROM ingest_jobs "
                "WHERE tenant_id = :t AND kb_id = :kb AND doc_id = :d "
                "AND status IN ('queued', 'running') "
                "ORDER BY id DESC LIMIT 1 FOR UPDATE"
            ),
            {"t": tenant_id, "kb": kb_id, "d": doc_id},
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)

        return int(
            connection.execute(
                text(
                    "INSERT INTO ingest_jobs "
                    "(tenant_id, kb_id, doc_id, status, error, source, attempts, progress) "
                    "VALUES (:t, :kb, :d, 'queued', '', :source, 0, 0) "
                    "RETURNING id"
                ),
                {"t": tenant_id, "kb": kb_id, "d": doc_id, "source": source},
            ).scalar_one()
        )


def claim_next(engine: Engine, *, worker: str = "w1") -> dict | None:
    """原子领取一条 queued 任务并置为 running；没有可领任务返回 None。

    `worker` 目前仅用于调用方标识 worker，任务表暂无对应字段。
    """
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT id FROM ingest_jobs "
                "WHERE status = 'queued' "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        ).mappings().first()
        if row is None:
            return None

        claimed = connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = 'running', progress = 10, attempts = attempts + 1, "
                "updated_at = now() "
                f"WHERE id = :id RETURNING {_JOB_COLUMNS}"
            ),
            {"id": row["id"]},
        ).mappings().one()

    return dict(claimed)


def mark_progress(engine: Engine, job_id: int, progress: int) -> None:
    """更新任务进度（0-100）与 updated_at。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET progress = :p, updated_at = now() WHERE id = :id"
            ),
            {"p": progress, "id": job_id},
        )


def mark_succeeded(engine: Engine, job_id: int) -> None:
    """标记成功：progress=100、error 清空、写入 finished_at。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = 'succeeded', progress = 100, error = '', "
                "finished_at = now(), updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": job_id},
        )


def mark_failed(engine: Engine, job_id: int, error: str) -> None:
    """记录失败：未达重试上限回到 queued；达到上限进入终态 failed。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = CASE WHEN attempts < :max THEN 'queued' ELSE 'failed' END, "
                "error = :error, "
                "finished_at = CASE WHEN attempts < :max THEN NULL ELSE now() END, "
                "updated_at = now() "
                "WHERE id = :id"
            ),
            {"max": MAX_ATTEMPTS, "error": error, "id": job_id},
        )


def get_job(engine: Engine, tenant_id: str, job_id: int) -> dict | None:
    """按租户查询单条任务；任务不存在或属于其他租户时返回 None。"""
    with engine.begin() as connection:
        row = connection.execute(
            text(
                f"SELECT {_JOB_COLUMNS} FROM ingest_jobs "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": job_id, "t": tenant_id},
        ).mappings().first()

    return dict(row) if row is not None else None


def list_jobs(
    engine: Engine, tenant_id: str, kb_id: str, limit: int = 20
) -> list[dict]:
    """按 id 倒序返回指定租户/知识库的最近任务。"""
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                f"SELECT {_JOB_COLUMNS} FROM ingest_jobs "
                "WHERE tenant_id = :t AND kb_id = :kb "
                "ORDER BY id DESC LIMIT :limit"
            ),
            {"t": tenant_id, "kb": kb_id, "limit": limit},
        ).mappings().all()

    return [dict(row) for row in rows]
