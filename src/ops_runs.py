"""调测运行记录（ops_runs）只读查询。

本期只提供读取；入队与状态机在后续任务实现。所有查询都带 tenant_id，
避免跨租户读到别人的运行记录。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

_RUN_COLUMNS = (
    "id, tenant_id, kb_id, kind, status, params, requested_by, "
    "requested_at, started_at, finished_at, exit_code, summary, "
    "artifact_path, output_tail, error, attempts"
)


def list_runs(
    engine: Engine, tenant_id: str, kb_id: str, *, limit: int = 20,
    kind: str | None = None,
) -> list[dict]:
    query = (
        f"SELECT {_RUN_COLUMNS} "
        "FROM ops_runs WHERE tenant_id = :t AND kb_id = :kb"
    )
    params: dict[str, object] = {"t": tenant_id, "kb": kb_id, "limit": limit}
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
