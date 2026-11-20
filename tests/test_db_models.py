"""表结构测试：一部分不连库（校验模型定义），一部分连库（校验真实 schema 与约束）。"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db import models  # noqa: F401  # 导入以注册所有表
from src.db.base import Base, create_db_engine
from src.db.models import EMBEDDING_DIM

EXPECTED_TABLES = {
    "tenants",
    "knowledge_bases",
    "documents",
    "chunks",
    "kb_versions",
    "agent_kb_bindings",
    "ingest_jobs",
    "users",
    "ops_runs",
    "ops_audit",
    "ingest_job_events",
    "ops_workers",
    "ops_http_metrics",
    "ops_rag_metrics",
}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_chunks_required_columns() -> None:
    columns = set(Base.metadata.tables["chunks"].c.keys())
    required = {
        "chunk_id",
        "tenant_id",
        "kb_id",
        "doc_id",
        "content_hash",
        "text",
        "token_count",
        "embedding",
        "meta",
        "embed_model_version",
    }
    assert required <= columns


def test_embedding_is_1024_dim_vector() -> None:
    embedding = Base.metadata.tables["chunks"].c.embedding
    assert embedding.type.dim == EMBEDDING_DIM == 1024
    # pgvector 的类型名是 VECTOR，避免误写成 ARRAY(Float)
    assert type(embedding.type).__name__ == "VECTOR"


def test_chunks_dedup_constraint_matches_local_store() -> None:
    names = {constraint.name for constraint in Base.metadata.tables["chunks"].constraints}
    assert "uq_chunks_tenant_kb_content_hash" in names


def test_tenant_kb_indexes_exist() -> None:
    index_names = {index.name for index in Base.metadata.tables["chunks"].indexes}
    assert "ix_chunks_tenant_id_kb_id" in index_names
    assert "ix_chunks_tenant_id_kb_id_doc_id" in index_names


def test_ops_rag_metric_stage_spans_server_default() -> None:
    stage_spans = Base.metadata.tables["ops_rag_metrics"].c.stage_spans
    assert stage_spans.default is not None
    assert stage_spans.server_default is not None
    assert str(stage_spans.server_default.arg) == "'[]'::jsonb"


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001  # 环境未启动容器时跳过，不阻塞单元测试
        pytest.skip(f"数据库不可用，跳过集成测试：{exc}")
    yield db_engine
    db_engine.dispose()


def test_migration_applied(engine) -> None:
    """确认 alembic 已经跑过，且 embedding 列真的是 vector(1024)。"""
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        column_type = connection.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        ).scalar_one()

    # 动态读取迁移脚本的 head，避免每加一个迁移就改测试
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert revision == head
    assert column_type == "vector(1024)"


def test_wrong_dimension_rejected(engine) -> None:
    """用 64 维的 mock 向量插正式表必须报错——这是维度契约的兜底。"""
    with engine.begin() as connection:
        # 声明作用域，确保接下来失败的原因是"维度不对"而不是 RLS 拦截
        connection.execute(
            text("SELECT set_config('app.tenant_id', 't_dim', true)")
        )
        connection.execute(
            text(
                "INSERT INTO tenants (id, name) VALUES ('t_dim', 'dim test') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES ('kb_dim', 't_dim', 'dim kb', '', 'active') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES ('d_dim', 't_dim', 'kb_dim', 'dim doc', '', 'hash_dim', 'active', '{}'::jsonb) "
                "ON CONFLICT (id) DO NOTHING"
            )
        )

        savepoint = connection.begin_nested()
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    "INSERT INTO chunks (chunk_id, tenant_id, kb_id, doc_id, content_hash, text, "
                    "token_count, embedding, embed_model_version, meta) "
                    "VALUES ('c_dim', 't_dim', 'kb_dim', 'd_dim', 'hash_dim', 'x', 1, "
                    "CAST(:vec AS vector), 'mock', '{}'::jsonb)"
                ),
                {"vec": "[0.1,0.2,0.3]"},
            )
        savepoint.rollback()

        # 级联删除，避免污染后续测试
        connection.execute(text("DELETE FROM tenants WHERE id = 't_dim'"))
