"""HTTP/RAG 原始指标。Revision ID: 0011, Revises: 0010."""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_http_metrics (
            id bigserial PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            tenant_id text NOT NULL,
            kb_id text,
            route text NOT NULL,
            method text NOT NULL,
            status_code smallint NOT NULL CHECK (status_code BETWEEN 100 AND 599),
            duration_ms double precision NOT NULL CHECK (duration_ms >= 0),
            trace_id text
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_http_metrics_tenant_time "
        "ON ops_http_metrics (tenant_id, occurred_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_http_metrics_tenant_kb_time "
        "ON ops_http_metrics (tenant_id, kb_id, occurred_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_rag_metrics (
            id bigserial PRIMARY KEY,
            trace_id text NOT NULL,
            tenant_id text NOT NULL,
            kb_id text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            operation text NOT NULL CHECK (operation IN ('search', 'chat')),
            mode text NOT NULL,
            outcome text NOT NULL CHECK (
                outcome IN ('succeeded', 'refused', 'error', 'cancelled')
            ),
            hits integer NOT NULL CHECK (hits >= 0),
            citation_ok boolean,
            rerank_degraded boolean,
            total_ms double precision NOT NULL CHECK (total_ms >= 0),
            rewrite_ms double precision CHECK (rewrite_ms >= 0),
            retrieval_ms double precision CHECK (retrieval_ms >= 0),
            rerank_ms double precision CHECK (rerank_ms >= 0),
            llm_ms double precision CHECK (llm_ms >= 0),
            model text,
            prompt_tokens integer CHECK (prompt_tokens >= 0),
            completion_tokens integer CHECK (completion_tokens >= 0),
            error_kind text,
            stage_spans jsonb NOT NULL DEFAULT '[]'::jsonb,
            CONSTRAINT uq_ops_rag_metrics_tenant_trace UNIQUE (tenant_id, trace_id),
            CONSTRAINT ck_ops_rag_metrics_spans_array
                CHECK (jsonb_typeof(stage_spans) = 'array')
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_rag_metrics_tenant_kb_time "
        "ON ops_rag_metrics (tenant_id, kb_id, occurred_at DESC, id DESC)"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE "
        "ops_http_metrics_id_seq, ops_rag_metrics_id_seq TO kb_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO kb_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops_rag_metrics")
    op.execute("DROP TABLE IF EXISTS ops_http_metrics")
