"""Alembic 运行环境：连接串从 src.db.base.database_url() 读取。

这样本地（DB_HOST/DB_PORT）和 CI（DATABASE_URL）用同一套代码，
不需要为每种环境维护一份 alembic.ini。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import models  # noqa: F401  # 必须导入，否则 Base.metadata 是空的
from src.db.base import Base, database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 百分号在 ini 配置里是插值语法，密码中若含 % 需要转义
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
