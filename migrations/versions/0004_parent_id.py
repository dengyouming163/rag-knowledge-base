"""父子分块：chunks 增加 parent_id 与 is_parent

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19

设计取舍：
- 子块（is_parent=false）参与向量与 BM25 检索，粒度小、命中更准；
- 父块（is_parent=true）不计算 embedding，只用于命中后补全上下文；
- 父块与子块同表：删除文档时一条 DELETE 全部清掉，不会出现"子删了父还在"。
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chunks ADD COLUMN parent_id VARCHAR(160)")
    op.execute("ALTER TABLE chunks ADD COLUMN is_parent BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        "CREATE INDEX ix_chunks_tenant_id_kb_id_parent_id "
        "ON chunks (tenant_id, kb_id, parent_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_tenant_id_kb_id_parent_id")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS is_parent")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS parent_id")
