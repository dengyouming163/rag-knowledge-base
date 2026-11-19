"""新增 kb_bm25：每个知识库一份倒排快照

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19

为什么按"每库一份整包快照"存，而不是把倒排拆成关系表：
- BM25 的倒排表是 (词 → chunk_id → 词频) 的稀疏嵌套结构，拆表后一次查询要 join 多次；
- 快照可直接整体加载进内存，重建时整包替换，天然原子；
- version 单调递增，缓存用它判断"我手里这份是不是旧的"。
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kb_bm25 (
            kb_id      VARCHAR(64) PRIMARY KEY,
            tenant_id  VARCHAR(64) NOT NULL,
            version    INTEGER NOT NULL DEFAULT 1,
            payload    JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_kb_bm25_tenant_id ON kb_bm25 (tenant_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kb_bm25")
