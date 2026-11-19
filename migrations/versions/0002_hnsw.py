"""为 chunks.embedding 建 HNSW 索引

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19

参数说明：
- m=16：每个节点的邻居数，越大召回越高、内存越多
- ef_construction=100：建索引时的候选队列长度，越大越准、建得越慢
- vector_cosine_ops：与 `<=>` 余弦距离配套；若改用内积需换成 vector_ip_ops
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
