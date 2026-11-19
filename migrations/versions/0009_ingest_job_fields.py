"""入库任务进度：ingest_jobs.source / attempts / progress / finished_at

source 区分上传/网页/API 等来源，attempts+progress 供 worker 重试与前端展示，
finished_at 标记终态时间（成功或失败），便于清理与统计耗时。

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT ''")
    op.execute(
        "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS progress integer NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz")


def downgrade() -> None:
    for column in ("source", "attempts", "progress", "finished_at"):
        op.execute(f"ALTER TABLE ingest_jobs DROP COLUMN IF EXISTS {column}")
