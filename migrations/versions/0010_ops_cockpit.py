"""调测驾驶舱：运行记录、审计、任务事件与 worker 心跳。

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_runs (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            kb_id text NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN (
                'eval_mock', 'loadtest', 'drill_parser', 'drill_llm', 'drill_queue'
            )),
            status text NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed'
            )),
            params jsonb NOT NULL DEFAULT '{}'::jsonb,
            requested_by text NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            exit_code integer,
            summary jsonb,
            artifact_path text,
            output_tail text,
            error text,
            attempts integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ops_runs_active_kind "
        "ON ops_runs (tenant_id, kb_id, kind) "
        "WHERE status IN ('queued', 'running')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_runs_tenant_kb_id "
        "ON ops_runs (tenant_id, kb_id, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_audit (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL,
            actor text NOT NULL,
            action text NOT NULL,
            target text,
            result text NOT NULL CHECK (result IN (
                'allowed', 'denied', 'succeeded', 'failed'
            )),
            reason text,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_audit_tenant_id "
        "ON ops_audit (tenant_id, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_job_events (
            id bigserial PRIMARY KEY,
            job_id bigint NOT NULL REFERENCES ingest_jobs(id) ON DELETE CASCADE,
            tenant_id text NOT NULL,
            stage text NOT NULL CHECK (stage IN (
                'queued', 'claimed', 'parsing', 'chunking',
                'embedding', 'publishing', 'succeeded', 'failed'
            )),
            status text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
            progress integer,
            message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_job_events_job_id "
        "ON ingest_job_events (job_id, id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_workers (
            worker_id text PRIMARY KEY,
            last_seen timestamptz NOT NULL,
            version text NOT NULL DEFAULT '',
            current_job_id bigint,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS started_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE ingest_jobs DROP COLUMN IF EXISTS started_at")
    op.execute("DROP TABLE IF EXISTS ops_workers")
    op.execute("DROP TABLE IF EXISTS ingest_job_events")
    op.execute("DROP TABLE IF EXISTS ops_audit")
    op.execute("DROP TABLE IF EXISTS ops_runs")
