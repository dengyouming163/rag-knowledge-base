"""FastAPI 应用入口。

异常映射的原则：
- 用户输入错误 → 4xx（明确告诉对方哪里错了）
- 代码缺陷 → 5xx（不推卸给调用方，也不泄露内部细节）
- 越权/不存在 → 404（不泄露"这个资源存在"）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from src.db.session import tenant_scope
from src.store_pg import MissingTenantFilterError

from .deps import get_engine
from .errors import ScopeNotFoundError
from .http_metrics import HttpMetricsMiddleware
from .routers import (
    chat,
    documents,
    health,
    identity,
    jobs,
    ops,
    search,
    stats,
    traces,
)

logger = logging.getLogger(__name__)

_drift_checked = False


def _warn_embedding_drift() -> None:
    """每进程只查一次；任何失败静默，启动自检不能拖垮服务。"""
    global _drift_checked
    if _drift_checked:
        return
    _drift_checked = True
    configured = os.environ.get("EMBED_MODEL", "").strip()
    if not configured:
        return
    try:
        engine = get_engine()
        with engine.connect() as c:
            tenants = [row[0] for row in c.execute(text("SELECT id FROM tenants"))]
        drift = 0
        for tenant in tenants:
            with tenant_scope(engine, tenant) as conn:
                drift += conn.execute(
                    text(
                        "SELECT count(*) FROM chunks "
                        "WHERE embed_model_version IS DISTINCT FROM :v"
                    ),
                    {"v": configured},
                ).scalar_one()
        if drift:
            logger.warning(
                "检测到 %s 个块与 EMBED_MODEL=%s 不一致；"
                "运行 py -3.13 scripts/reembed_chunks.py --tenant <t> 重嵌",
                drift,
                configured,
            )
    except Exception:  # noqa: BLE001
        logger.debug("embedding drift check skipped", exc_info=True)


def create_app() -> FastAPI:
    app = FastAPI(title="RAG Knowledge Base", version="0.1.0")
    app.add_middleware(HttpMetricsMiddleware)
    # 默认不启用跨域：只有显式配置白名单时才放行，避免把控制台/接口暴露给任意站点。
    origins = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-Tenant-Id", "X-User-Id", "Authorization"],
            max_age=600,
        )
    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(traces.router)
    app.include_router(chat.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)
    app.include_router(stats.router)
    app.include_router(identity.router)
    app.include_router(ops.router)
    app.mount(
        "/ui",
        StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True),
        name="ui",
    )

    @app.middleware("http")
    async def ui_no_cache(request: Request, call_next):
        # 控制台没有构建步骤，静态文件随源码更新；强制浏览器每次回源校验，
        # 避免旧的字典/视图模块被缓存后界面显示原始 i18n key。
        response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(MissingTenantFilterError)
    async def missing_scope_handler(request: Request, exc: MissingTenantFilterError):
        # 这是代码 bug（忘了传租户），不是用户错误：必须 500 并留下日志
        print(f"[ERROR] 缺少租户/知识库过滤条件，path={request.url.path}：{exc}")
        return JSONResponse(status_code=500, content={"detail": "internal scope error"})

    @app.exception_handler(ScopeNotFoundError)
    async def not_found_handler(request: Request, exc: ScopeNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "knowledge base not found"})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        trace_id = getattr(request.state, "trace_id", None)
        headers = {"X-Trace-Id": trace_id} if trace_id else None
        return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)

    _warn_embedding_drift()
    return app


app = create_app()
