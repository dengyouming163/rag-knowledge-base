"""知识库版本：发布、回滚、按版本固定检索集合。

设计要点：
- 快照只存 chunk_id 列表与摘要，不复制正文——正文由 chunks 表唯一持有。
- 发布是"登记 + 切换指针"，不是"复制数据"，因此秒级完成。
- 回滚 = 把 active_version_id 指回旧版本；检索时把该版本的 chunk_ids 传给
  store，即可把可见集合钉在旧快照上（新增的块自然被排除）。
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db.session import tenant_scope


class VersionNotFoundError(LookupError):
    """版本不存在或不属于当前租户/知识库。"""


def _snapshot_digest(chunk_ids: list[str]) -> str:
    payload = json.dumps(sorted(chunk_ids), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def publish(engine: Engine, tenant_id: str, kb_id: str, note: str = "") -> dict:
    """给当前内容打一个快照并发布，返回版本记录。"""
    with tenant_scope(engine, tenant_id, write=True) as connection:
        chunk_ids = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT chunk_id FROM chunks "
                    "WHERE tenant_id = :t AND kb_id = :kb AND is_parent = false "
                    "ORDER BY chunk_id"
                ),
                {"t": tenant_id, "kb": kb_id},
            )
        ]
        if not chunk_ids:
            raise ValueError(f"知识库 {kb_id} 没有可发布的 chunk")

        next_version = (
            connection.execute(
                text(
                    "SELECT coalesce(max(version), 0) + 1 FROM kb_versions "
                    "WHERE tenant_id = :t AND kb_id = :kb"
                ),
                {"t": tenant_id, "kb": kb_id},
            ).scalar_one()
        )

        connection.execute(
            text(
                "UPDATE kb_versions SET status = 'superseded' "
                "WHERE tenant_id = :t AND kb_id = :kb AND status = 'published'"
            ),
            {"t": tenant_id, "kb": kb_id},
        )

        snapshot = {
            "chunk_ids": chunk_ids,
            "chunk_count": len(chunk_ids),
            "digest": _snapshot_digest(chunk_ids),
            "note": note,
        }
        version_id = connection.execute(
            text(
                "INSERT INTO kb_versions (tenant_id, kb_id, version, status, snapshot, published_at) "
                "VALUES (:t, :kb, :v, 'published', CAST(:snapshot AS jsonb), now()) "
                "RETURNING id"
            ),
            {
                "t": tenant_id,
                "kb": kb_id,
                "v": next_version,
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
            },
        ).scalar_one()

        connection.execute(
            text(
                "UPDATE knowledge_bases SET active_version_id = :vid "
                "WHERE id = :kb AND tenant_id = :t"
            ),
            {"vid": version_id, "kb": kb_id, "t": tenant_id},
        )

    return {
        "version_id": version_id,
        "version": next_version,
        "chunk_count": len(chunk_ids),
        "digest": snapshot["digest"],
    }


def rollback(engine: Engine, tenant_id: str, kb_id: str, version_id: int) -> dict:
    """把 active 指针切回指定历史版本。"""
    with tenant_scope(engine, tenant_id, write=True) as connection:
        row = connection.execute(
            text(
                "SELECT id, version, snapshot FROM kb_versions "
                "WHERE id = :vid AND tenant_id = :t AND kb_id = :kb"
            ),
            {"vid": version_id, "t": tenant_id, "kb": kb_id},
        ).mappings().first()
        if row is None:
            raise VersionNotFoundError(f"版本 {version_id} 不属于 {kb_id}")

        connection.execute(
            text(
                "UPDATE kb_versions SET status = 'superseded' "
                "WHERE tenant_id = :t AND kb_id = :kb AND status = 'published'"
            ),
            {"t": tenant_id, "kb": kb_id},
        )
        connection.execute(
            text("UPDATE kb_versions SET status = 'published' WHERE id = :vid"),
            {"vid": version_id},
        )
        connection.execute(
            text(
                "UPDATE knowledge_bases SET active_version_id = :vid "
                "WHERE id = :kb AND tenant_id = :t"
            ),
            {"vid": version_id, "kb": kb_id, "t": tenant_id},
        )

    snapshot = row["snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    return {
        "version_id": row["id"],
        "version": row["version"],
        "chunk_count": snapshot.get("chunk_count", 0),
        "chunk_ids": snapshot.get("chunk_ids", []),
    }


def active_version_chunk_ids(engine: Engine, tenant_id: str, kb_id: str) -> list[str]:
    """返回当前发布版本的 chunk_ids，用于把检索固定在该版本。"""
    with tenant_scope(engine, tenant_id) as connection:
        row = connection.execute(
            text(
                "SELECT snapshot FROM kb_versions "
                "WHERE tenant_id = :t AND kb_id = :kb AND status = 'published'"
            ),
            {"t": tenant_id, "kb": kb_id},
        ).mappings().first()

    if row is None:
        return []
    snapshot = row["snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    return list(snapshot.get("chunk_ids", []))
