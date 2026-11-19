"""初始化企业知识库表结构（含 pgvector 扩展）

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    # 扩展是按数据库生效的：卷被重建后必须能靠这条迁移恢复
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
    )

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_knowledge_bases_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_bases"),
    )
    op.create_index(
        "ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"], unique=False
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_documents_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_documents_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint(
            "tenant_id",
            "kb_id",
            "content_hash",
            name="uq_documents_tenant_kb_content_hash",
        ),
    )
    op.create_index(
        "ix_documents_tenant_id_kb_id", "documents", ["tenant_id", "kb_id"], unique=False
    )

    op.create_table(
        "chunks",
        sa.Column("chunk_id", sa.String(length=160), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "embedding", pgvector.sqlalchemy.Vector(dim=EMBEDDING_DIM), nullable=True
        ),
        sa.Column("embed_model_version", sa.String(length=64), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_chunks_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_chunks_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["documents.id"],
            name="fk_chunks_doc_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_chunks"),
        sa.UniqueConstraint(
            "tenant_id",
            "kb_id",
            "content_hash",
            name="uq_chunks_tenant_kb_content_hash",
        ),
    )
    op.create_index(
        "ix_chunks_tenant_id_kb_id", "chunks", ["tenant_id", "kb_id"], unique=False
    )
    op.create_index(
        "ix_chunks_tenant_id_kb_id_doc_id",
        "chunks",
        ["tenant_id", "kb_id", "doc_id"],
        unique=False,
    )

    op.create_table(
        "kb_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_kb_versions_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_kb_versions_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_versions"),
        sa.UniqueConstraint("kb_id", "version", name="uq_kb_versions_kb_version"),
    )
    op.create_index(
        "ix_kb_versions_tenant_id_kb_id", "kb_versions", ["tenant_id", "kb_id"], unique=False
    )

    op.create_table(
        "agent_kb_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("agent_card_id", sa.String(length=128), nullable=False),
        sa.Column("kb_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_agent_kb_bindings_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_agent_kb_bindings_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_kb_bindings"),
        sa.UniqueConstraint(
            "tenant_id",
            "agent_card_id",
            "kb_id",
            name="uq_agent_kb_bindings_tenant_agent_kb",
        ),
    )
    op.create_index(
        "ix_agent_kb_bindings_tenant_id_agent_card_id",
        "agent_kb_bindings",
        ["tenant_id", "agent_card_id"],
        unique=False,
    )

    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("kb_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_ingest_jobs_tenant_id_tenants",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name="fk_ingest_jobs_kb_id_knowledge_bases",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingest_jobs"),
    )
    op.create_index(
        "ix_ingest_jobs_tenant_id_kb_id", "ingest_jobs", ["tenant_id", "kb_id"], unique=False
    )
    op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status", table_name="ingest_jobs")
    op.drop_index("ix_ingest_jobs_tenant_id_kb_id", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")

    op.drop_index(
        "ix_agent_kb_bindings_tenant_id_agent_card_id", table_name="agent_kb_bindings"
    )
    op.drop_table("agent_kb_bindings")

    op.drop_index("ix_kb_versions_tenant_id_kb_id", table_name="kb_versions")
    op.drop_table("kb_versions")

    op.drop_index("ix_chunks_tenant_id_kb_id_doc_id", table_name="chunks")
    op.drop_index("ix_chunks_tenant_id_kb_id", table_name="chunks")
    op.drop_table("chunks")

    op.drop_index("ix_documents_tenant_id_kb_id", table_name="documents")
    op.drop_table("documents")

    op.drop_index("ix_knowledge_bases_tenant_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")

    op.drop_table("tenants")
    # 故意不 DROP EXTENSION：可能还有别的库/表依赖它
