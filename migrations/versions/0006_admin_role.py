"""管理员角色：users.role

为什么单独一列而不是用 `role:admin` 标签：
标签是"内容可见性"的维度，角色是"能跳过可见性"的能力，两者混在一起会出现
"给自己打一个 role:admin 标签就变成管理员"的提权路径。角色必须独立、且只能由
服务端从用户表读取，不能来自请求头或请求体。

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'member'"
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_role CHECK (role IN ('member', 'admin'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS role")
