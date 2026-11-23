"""调测运行记录（ops_runs）：只读查询与入队/领取/终态状态机。

状态流转：
    queued -> running -> succeeded
                      -> failed

同一租户+知识库+kind 至多一个活跃运行（queued/running），由数据库局部唯一
索引保证；冲突时 `enqueue` 抛 `AlreadyRunning` 而不是静默复用。
所有查询都带 tenant_id，避免跨租户读到别人的运行记录。
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

_RUN_COLUMNS = (
    "id, tenant_id, kb_id, kind, status, params, requested_by, "
    "requested_at, started_at, finished_at, exit_code, summary, "
    "artifact_path, output_tail, error, attempts"
)

# 列表接口不返回 output_tail：日志尾部可能很长，列表只需摘要。
_RUN_LIST_COLUMNS = (
    "id, tenant_id, kb_id, kind, status, params, requested_by, "
    "requested_at, started_at, finished_at, exit_code, summary, "
    "artifact_path, error, attempts"
)


class AlreadyRunning(Exception):
    """同一 kind 已有 queued/running 运行。"""

    def __init__(self, run_id: int):
        super().__init__(f"run already active: {run_id}")
        self.run_id = run_id


def list_runs(
    engine: Engine, tenant_id: str, kb_id: str, *, limit: int = 20,
    kind: str | None = None,
) -> list[dict]:
    query = (
        f"SELECT {_RUN_LIST_COLUMNS} "
        "FROM ops_runs WHERE tenant_id = :t AND kb_id = :kb"
    )
    params: dict[str, object] = {
        "t": tenant_id, "kb": kb_id, "limit": max(1, min(limit, 200)),
    }
    if kind:
        query += " AND kind = :kind"
        params["kind"] = kind
    query += " ORDER BY id DESC LIMIT :limit"
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).mappings().all()
    return [dict(row) for row in rows]


def get_run(engine: Engine, tenant_id: str, run_id: int) -> dict | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT {_RUN_COLUMNS} FROM ops_runs "
                "WHERE id = :id AND tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id},
        ).mappings().first()
    return dict(row) if row else None


def enqueue(
    engine: Engine, tenant_id: str, kb_id: str, kind: str, *,
    requested_by: str, params: dict | None = None,
) -> int:
    """入队一条运行；同 kind 已有活跃运行时抛 `AlreadyRunning`。"""
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "INSERT INTO ops_runs "
                    "(tenant_id, kb_id, kind, status, params, requested_by) "
                    "VALUES (:t, :kb, :kind, 'queued', CAST(:params AS jsonb), :by) "
                    "RETURNING id"
                ),
                {
                    "t": tenant_id, "kb": kb_id, "kind": kind,
                    "params": json.dumps(params or {}, ensure_ascii=False),
                    "by": requested_by,
                },
            ).scalar_one()
        return int(row)
    except IntegrityError as exc:
        # 只把「活跃 kind 唯一索引」冲突翻译成 AlreadyRunning；
        # 找不到活跃记录说明是其它完整性错误（如外键），原样抛出。
        with engine.connect() as connection:
            existing = connection.execute(
                text(
                    "SELECT id FROM ops_runs "
                    "WHERE tenant_id = :t AND kb_id = :kb AND kind = :kind "
                    "AND status IN ('queued','running') ORDER BY id LIMIT 1"
                ),
                {"t": tenant_id, "kb": kb_id, "kind": kind},
            ).scalar_one_or_none()
        if existing is None:
            raise
        raise AlreadyRunning(int(existing)) from exc


def claim_next(engine: Engine, *, kind: str | None = None) -> dict | None:
    """原子领取一条 queued 运行并置为 running；没有可领记录返回 None。"""
    query = "SELECT id FROM ops_runs WHERE status = 'queued'"
    params: dict[str, object] = {}
    if kind:
        query += " AND kind = :kind"
        params["kind"] = kind
    query += " ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"

    with engine.begin() as connection:
        row = connection.execute(text(query), params).first()
        if row is None:
            return None
        claimed = connection.execute(
            text(
                "UPDATE ops_runs SET status = 'running', attempts = attempts + 1, "
                "started_at = now() WHERE id = :id AND status = 'queued' "
                f"RETURNING {_RUN_COLUMNS}"
            ),
            {"id": row[0]},
        ).mappings().one()
    return dict(claimed)


def mark_succeeded(
    engine: Engine, run_id: int, *, exit_code: int, summary: dict,
    artifact_path: str | None, output_tail: str,
) -> None:
    """仅 running 可转终态；其它状态（queued/succeeded/failed）静默忽略。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_runs SET status='succeeded', exit_code=:code, "
                "summary=CAST(:summary AS jsonb), artifact_path=:path, "
                "output_tail=:tail, finished_at=now(), error=NULL "
                "WHERE id=:id AND status='running'"
            ),
            {
                "id": run_id, "code": exit_code,
                "summary": json.dumps(summary, ensure_ascii=False),
                "path": artifact_path, "tail": output_tail[-8192:],
            },
        )


def mark_failed(
    engine: Engine, run_id: int, *, error: str, exit_code: int | None,
    summary: dict | None = None, artifact_path: str | None = None,
    output_tail: str = "",
) -> None:
    """仅 running 可转终态；其它状态（queued/succeeded/failed）静默忽略。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_runs SET status='failed', exit_code=:code, "
                "summary=CAST(:summary AS jsonb), artifact_path=:path, "
                "output_tail=:tail, error=:error, finished_at=now() "
                "WHERE id=:id AND status='running'"
            ),
            {
                "id": run_id, "code": exit_code,
                "summary": json.dumps(summary or {}, ensure_ascii=False),
                "path": artifact_path, "tail": output_tail[-8192:],
                "error": error[-2000:],
            },
        )
