"""chunks 表启用行级安全（RLS）

目的：把"每条查询都要记得写 tenant_id"从编码约定变成数据库强制。
即使将来有人新写一条查询忘了加 WHERE，也只会返回 0 行，而不是别的租户的数据。

只对 chunks 开启（本阶段）。documents / knowledge_bases 仍靠应用层过滤，
跑通后再逐步扩。

策略用 `current_setting('app.tenant_id', true)`：
- true 表示"取不到就返回 NULL"，而不是直接报错；
- NULL 参与比较结果为假 → 0 行，属于 fail-closed。

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 超级用户会绕过 RLS，所以业务侧必须用独立角色
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app') THEN
                CREATE ROLE kb_app LOGIN PASSWORD 'kb_app' NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO kb_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO kb_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kb_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kb_app"
    )

    op.execute("ALTER TABLE chunks ENABLE ROW LEVEL SECURITY")
    # FORCE 让表属主也受策略约束；业务进程用 kb_app（非属主、非超级用户）
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_chunks_tenant ON chunks")
    op.execute(
        """
        CREATE POLICY p_chunks_tenant ON chunks
        USING (tenant_id = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS p_chunks_tenant ON chunks")
    op.execute("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY")
