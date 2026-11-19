"""数据库连接基座：统一的 Base、URL 解析与 engine 构造。

URL 优先级：环境变量 DATABASE_URL > 由 DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME 拼装。
这样 CI 里可以直接给一个 DATABASE_URL，本地开发用 .env.example 里的默认值。
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

# 统一约束命名，避免 Alembic 在不同环境下生成随机约束名导致无法回滚
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def database_url() -> str:
    """返回 SQLAlchemy 连接串，驱动固定用 psycopg（v3）。"""
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = os.environ.get("DB_PORT", "5433")
    user = os.environ.get("DB_USER", "kb")
    password = os.environ.get("DB_PASSWORD", "kb")
    name = os.environ.get("DB_NAME", "kb")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def app_database_url() -> str:
    """应用连接串：默认使用非超级用户 kb_app。

    超级用户（POSTGRES_USER=kb 建出来的那个）会绕过 RLS，
    所以业务进程必须换一个普通角色，行级安全才真正生效。
    迁移仍用 database_url()，因为建表/建角色需要属主权限。
    """
    explicit = os.environ.get("DATABASE_URL_APP")
    if explicit:
        return explicit

    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = os.environ.get("DB_PORT", "5433")
    user = os.environ.get("DB_APP_USER", "kb_app")
    password = os.environ.get("DB_APP_PASSWORD", "kb_app")
    name = os.environ.get("DB_NAME", "kb")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def create_db_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """pool_pre_ping 会在取连接前探活，避免容器重启后拿到失效连接。"""
    return create_engine(url or app_database_url(), pool_pre_ping=True, **kwargs)
