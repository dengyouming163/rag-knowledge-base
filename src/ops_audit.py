"""运维审计：记录管理员动作的放行/拒绝与结果，按租户读取。

审计记录本身不带外键，租户被删除后仍保留（用于事后追溯）。
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

_AUDIT_COLUMNS = "id, tenant_id, actor, action, target, result, reason, detail, created_at"


def record(
    engine: Engine, *, tenant_id: str, actor: str, action: str,
    target: str | None, result: str, reason: str = "", detail: dict | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ops_audit "
                "(tenant_id, actor, action, target, result, reason, detail) "
                "VALUES (:t, :actor, :action, :target, :result, :reason, "
                "CAST(:detail AS jsonb))"
            ),
            {
                "t": tenant_id, "actor": actor, "action": action,
                "target": target, "result": result, "reason": reason,
                "detail": json.dumps(detail or {}, ensure_ascii=False),
            },
        )


def list_records(engine: Engine, tenant_id: str, *, limit: int = 50) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT {_AUDIT_COLUMNS} FROM ops_audit "
                "WHERE tenant_id = :t ORDER BY id DESC LIMIT :limit"
            ),
            {"t": tenant_id, "limit": max(1, min(limit, 200))},
        ).mappings().all()
    return [dict(row) for row in rows]
