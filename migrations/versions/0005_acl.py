"""文档级 ACL：users 表 + chunks.meta.acl_tags 索引

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19

设计取舍：
- 标签放在 `chunks.meta->'acl_tags'`，而不是新开一张 chunk_acl 表：
  ACL 与块同生同灭，删文档不会留下孤儿授权记录。
- 语义：数组为空或字段缺失 = 公开；非空则要求用户标签与之有交集。
- 只对含该字段的行建部分索引，避免给全部块付 GIN 的写入成本。
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id      VARCHAR(64) PRIMARY KEY,
            tenant_id    VARCHAR(64) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            display_name VARCHAR(128) NOT NULL DEFAULT '',
            tags         TEXT[] NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_users_tenant_id ON users (tenant_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_acl_tags "
        "ON chunks USING gin ((meta -> 'acl_tags')) "
        "WHERE meta ? 'acl_tags'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_acl_tags")
    op.execute("DROP INDEX IF EXISTS ix_users_tenant_id")
    op.execute("DROP TABLE IF EXISTS users")
