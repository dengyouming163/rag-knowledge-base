"""入库任务状态机：入队、领取、进度、成功、失败与重试。

状态流转：
    queued -> running -> succeeded
                      -> queued（未达重试上限）
                      -> failed（不可重试或达到 MAX_ATTEMPTS）

约定：所有返回的 dict 由 `dict(row._mapping)` 构造，时间字段（created_at /
updated_at / finished_at）保留为带时区的 datetime 对象，不转 ISO 字符串。

ingest_jobs 未开启 RLS，普通事务即可；读取 chunks 等受 RLS 保护的表时应由调用方
使用 tenant_scope。本模块所有按 id 操作的任务都属于 worker 内部流程，
查询接口 `get_job` / `list_jobs` 必须携带 tenant_id 做租户隔离。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .storage import LocalStorage, Storage, build_storage

STATUSES = ("queued", "running", "succeeded", "failed")
MAX_ATTEMPTS = 3
DEFAULT_JOB_LEASE_SECONDS = 900
ALLOWED_SOURCE_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

_JOB_COLUMNS = (
    "id, tenant_id, kb_id, doc_id, status, error, source, attempts, progress, "
    "started_at, finished_at, created_at, updated_at"
)


def _source_suffix(source_name: str, uri: str) -> str:
    if source_name == "feishu":
        return ".md"
    parts = urlsplit(uri)
    if parts.scheme != "file":
        raise ValueError(f"来源文档 URI 不是 file URI：{uri}")
    suffix = Path(parts.path).suffix.lower()
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        raise ValueError(f"不支持的来源文档后缀：{suffix or '(none)'}")
    return suffix


def _source_relative_path(
    tenant_id: str, kb_id: str, doc_id: str, suffix: str
) -> Path:
    return Path(tenant_id) / kb_id / f"{doc_id}{suffix}"


def receive_source_document(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    *,
    source_name: str,
    uri: str,
    content: bytes,
    upload_root: str | Path | None = None,
    storage: Storage | None = None,
) -> dict:
    """接收一份 Source 内容、落盘并复用 enqueue；返回新增/重复及 job 信息。

    content_hash、doc_id 与 source_url 均沿用上传 API 约定。ready 的重复内容
    不再建任务，其他重复内容复用 queued/running 任务或重新入队失败任务。
    """
    if not _SAFE_ID.fullmatch(tenant_id):
        raise ValueError("invalid tenant_id")
    if not _SAFE_ID.fullmatch(kb_id):
        raise ValueError("invalid kb_id")
    if not content:
        raise ValueError("来源文档内容为空")
    suffix = _source_suffix(source_name, uri)
    content_hash = hashlib.sha256(content).hexdigest()[:16]
    duplicated = False

    with engine.begin() as connection:
        existing = connection.execute(
            text(
                "SELECT id, status, source_url FROM documents "
                "WHERE tenant_id = :t AND kb_id = :kb AND content_hash = :hash"
            ),
            {"t": tenant_id, "kb": kb_id, "hash": content_hash},
        ).mappings().first()

        if existing is None:
            doc_id = f"{kb_id}-{uuid4().hex[:8]}"
            relative_path = _source_relative_path(tenant_id, kb_id, doc_id, suffix)
            inserted = connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:id, :t, :kb, :id, :url, :hash, 'pending', '{}'::jsonb) "
                    "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "id": doc_id,
                    "t": tenant_id,
                    "kb": kb_id,
                    "url": relative_path.as_posix(),
                    "hash": content_hash,
                },
            ).scalar_one_or_none()
            if inserted is None:
                existing = connection.execute(
                    text(
                        "SELECT id, status, source_url FROM documents "
                        "WHERE tenant_id = :t AND kb_id = :kb AND content_hash = :hash"
                    ),
                    {"t": tenant_id, "kb": kb_id, "hash": content_hash},
                ).mappings().one()

        if existing is not None:
            duplicated = True
            doc_id = existing["id"]
            existing_suffix = Path(existing["source_url"]).suffix.lower()
            if existing_suffix not in ALLOWED_SOURCE_SUFFIXES:
                raise ValueError(f"已有文档 source_url 后缀异常：{existing['source_url']}")
            relative_path = _source_relative_path(
                tenant_id, kb_id, doc_id, existing_suffix
            )
            if existing["source_url"] != relative_path.as_posix():
                raise ValueError(f"已有文档 source_url 路径异常：{existing['source_url']}")
            if existing["status"] == "ready":
                return {"doc_id": doc_id, "job_id": None, "duplicated": True}
            active_job_id = connection.execute(
                text(
                    "SELECT id FROM ingest_jobs "
                    "WHERE tenant_id = :t AND kb_id = :kb AND doc_id = :d "
                    "AND status IN ('queued', 'running') "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"t": tenant_id, "kb": kb_id, "d": doc_id},
            ).scalar_one_or_none()
            if active_job_id is not None:
                return {
                    "doc_id": doc_id,
                    "job_id": int(active_job_id),
                    "duplicated": True,
                }

    if storage is not None:
        resolved_storage: Storage = storage
    elif upload_root is not None:
        # upload_root 仅保留给测试与本地脚本显式指定目录，生产走 build_storage()
        resolved_storage = LocalStorage(upload_root)
    else:
        resolved_storage = build_storage()

    try:
        resolved_storage.put_bytes(relative_path.as_posix(), content)
        job_id = enqueue(engine, tenant_id, kb_id, doc_id, source=source_name)
    except Exception:
        with engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{tenant_id}:{kb_id}:{doc_id}"},
            )
            connection.execute(
                text(
                    "UPDATE documents AS d SET status = 'failed', updated_at = now() "
                    "WHERE d.id = :id AND d.tenant_id = :t AND d.kb_id = :kb "
                    "AND d.status <> 'ready' AND NOT EXISTS ("
                    "SELECT 1 FROM ingest_jobs j "
                    "WHERE j.tenant_id = d.tenant_id AND j.kb_id = d.kb_id "
                    "AND j.doc_id = d.id AND j.status IN ('queued', 'running'))"
                ),
                {"id": doc_id, "t": tenant_id, "kb": kb_id},
            )
        raise

    return {"doc_id": doc_id, "job_id": job_id, "duplicated": duplicated}


def enqueue(
    engine: Engine, tenant_id: str, kb_id: str, doc_id: str, *, source: str = "upload"
) -> int:
    """入队一条任务；同一文档已有 queued/running 任务时复用该任务 id。"""
    with engine.begin() as connection:
        # 事务级咨询锁：同一文档的并发入队串行化，避免 SELECT 与 INSERT 之间插入重复行
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{tenant_id}:{kb_id}:{doc_id}"},
        )
        existing = connection.execute(
            text(
                "SELECT id FROM ingest_jobs "
                "WHERE tenant_id = :t AND kb_id = :kb AND doc_id = :d "
                "AND status IN ('queued', 'running') "
                "ORDER BY id DESC LIMIT 1 FOR UPDATE"
            ),
            {"t": tenant_id, "kb": kb_id, "d": doc_id},
        ).scalar_one_or_none()
        connection.execute(
            text(
                "UPDATE documents SET status = 'pending', updated_at = now() "
                "WHERE id = :d AND tenant_id = :t AND kb_id = :kb AND status = 'failed'"
            ),
            {"d": doc_id, "t": tenant_id, "kb": kb_id},
        )
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


def job_lease_seconds() -> int:
    """允许用 JOB_LEASE_SECONDS 覆盖租约；非法值退回默认，避免配置错误导致误回收。"""
    raw = os.environ.get("JOB_LEASE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_JOB_LEASE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_JOB_LEASE_SECONDS
    return value if value > 0 else DEFAULT_JOB_LEASE_SECONDS


def reclaim_stale_running(engine: Engine, *, lease_seconds: int | None = None) -> int:
    """回收超过租约仍 running 的任务；attempts 已达 MAX_ATTEMPTS 则终态失败。"""
    lease = job_lease_seconds() if lease_seconds is None else lease_seconds
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "UPDATE ingest_jobs SET "
                "status = CASE WHEN attempts < :max THEN 'queued' ELSE 'failed' END, "
                "progress = CASE WHEN attempts < :max THEN 0 ELSE progress END, "
                "error = CASE WHEN attempts < :max THEN '' "
                "ELSE 'lease expired after ' || :lease || ' seconds' END, "
                "started_at = CASE WHEN attempts < :max THEN NULL ELSE started_at END, "
                "finished_at = CASE WHEN attempts < :max THEN NULL ELSE now() END, "
                "updated_at = now() "
                "WHERE status = 'running' "
                "AND updated_at < now() - make_interval(secs => :lease) "
                "RETURNING id, tenant_id, doc_id, status"
            ),
            {"max": MAX_ATTEMPTS, "lease": lease},
        ).mappings().all()
        for row in rows:
            if row["status"] == "failed":
                connection.execute(
                    text(
                        "UPDATE documents SET status = 'failed', updated_at = now() "
                        "WHERE id = :d AND tenant_id = :t AND status <> 'ready'"
                    ),
                    {"d": row["doc_id"], "t": row["tenant_id"]},
                )
    return len(rows)


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
                "started_at = COALESCE(started_at, now()), "
                "updated_at = now() "
                f"WHERE id = :id AND status = 'queued' RETURNING {_JOB_COLUMNS}"
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


def mark_failed(engine: Engine, job_id: int, error: str, *, retry: bool = True) -> None:
    """记录失败：可重试且未达上限回到 queued，否则进入终态 failed。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = CASE WHEN :retry AND attempts < :max "
                "THEN 'queued' ELSE 'failed' END, "
                "error = :error, "
                "finished_at = CASE WHEN :retry AND attempts < :max "
                "THEN NULL ELSE now() END, "
                "updated_at = now() "
                "WHERE id = :id"
            ),
            {"max": MAX_ATTEMPTS, "error": error, "id": job_id, "retry": retry},
        )


def retry_job(engine: Engine, tenant_id: str, job_id: int) -> dict | None:
    """只允许 failed 任务人工重试；重置 attempts/progress/error。

    任务回到 queued 的同时，把对应文档从 failed 改回 pending，避免任务在跑
    而文档仍显示失败。
    """
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = 'queued', attempts = 0, progress = 0, error = '', "
                "started_at = NULL, finished_at = NULL, updated_at = now() "
                "WHERE id = :id AND tenant_id = :t AND status = 'failed' "
                f"RETURNING {_JOB_COLUMNS}"
            ),
            {"id": job_id, "t": tenant_id},
        ).mappings().first()
        if row is None:
            return None
        connection.execute(
            text(
                "UPDATE documents SET status = 'pending', updated_at = now() "
                "WHERE id = :d AND tenant_id = :t AND kb_id = :kb AND status = 'failed'"
            ),
            {"d": row["doc_id"], "t": tenant_id, "kb": row["kb_id"]},
        )
    return dict(row)


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
    engine: Engine, tenant_id: str, kb_id: str, limit: int | None = 20
) -> list[dict]:
    """按 id 倒序返回指定租户/知识库的任务；limit=None 时返回全部。"""
    query = (
        f"SELECT {_JOB_COLUMNS} FROM ingest_jobs "
        "WHERE tenant_id = :t AND kb_id = :kb ORDER BY id DESC"
    )
    params: dict[str, str | int] = {"t": tenant_id, "kb": kb_id}
    if limit is not None:
        query += " LIMIT :limit"
        params["limit"] = limit

    with engine.begin() as connection:
        rows = connection.execute(text(query), params).mappings().all()

    return [dict(row) for row in rows]
