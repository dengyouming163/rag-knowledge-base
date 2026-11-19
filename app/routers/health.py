"""健康检查：存活探针与就绪探针分离。

- `/live` 只代表"进程还在"，任何情况下都返回 200。用它做重启判断，
  否则数据库抖动会导致容器被反复杀掉，故障被放大。
- `/ready` 代表"能对外服务"，需要探测数据库。结果缓存 5 秒，
  避免每次探针都打一次数据库；探测本身限时 2 秒，避免请求堆积。
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text

from src.db.base import database_url

router = APIRouter()

DB_TIMEOUT_SECONDS = 2
CACHE_TTL_SECONDS = 5

_cache: dict[str, float | bool | None] = {"expires_at": 0.0, "ok": None}


def _with_timeout(url: str) -> str:
    """给连接串补上 connect_timeout，避免数据库不响应时一直挂着。"""
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}connect_timeout={DB_TIMEOUT_SECONDS}"


def probe_db() -> bool:
    """真实探测：连一次库，执行 SELECT 1。"""
    engine = create_engine(_with_timeout(database_url()), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 任何异常都视为"不可用"
        return False
    finally:
        engine.dispose()


def reset_cache() -> None:
    _cache["expires_at"] = 0.0
    _cache["ok"] = None


@router.get("/live")
def live() -> dict[str, bool]:
    return {"ok": True}


@router.get("/ready")
def ready():
    now = time.monotonic()
    if _cache["ok"] is None or now >= float(_cache["expires_at"] or 0.0):
        _cache["ok"] = probe_db()
        _cache["expires_at"] = now + CACHE_TTL_SECONDS

    if not _cache["ok"]:
        return JSONResponse(status_code=503, content={"ok": False, "db": "down"})
    return {"ok": True, "db": "up"}


@router.get("/")
def index() -> dict[str, str]:
    return {"service": "rag-knowledge-base", "env": os.environ.get("APP_ENV", "local")}
