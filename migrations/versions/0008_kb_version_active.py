"""版本发布：knowledge_bases.active_version_id + 每库唯一 published 版本

回滚语义：把 active_version_id 指回历史版本，检索时可带上该版本的 chunk_ids
（`filters={"version_chunk_ids": [...]}`）把可见集合钉在旧快照上。

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS active_version_id BIGINT "
        "REFERENCES kb_versions(id) ON DELETE SET NULL"
    )
    # 同一个知识库同一时刻只能有一个已发布版本，靠数据库约束保证
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_versions_published "
        "ON kb_versions (kb_id) WHERE status = 'published'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_kb_versions_published")
    op.execute("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS active_version_id")
