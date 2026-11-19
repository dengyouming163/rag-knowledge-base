"""FastAPI 应用入口。

异常映射的原则：
- 用户输入错误 → 4xx（明确告诉对方哪里错了）
- 代码缺陷 → 5xx（不推卸给调用方，也不泄露内部细节）
- 越权/不存在 → 404（不泄露"这个资源存在"）
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.store_pg import MissingTenantFilterError

from .errors import ScopeNotFoundError
from .routers import chat, documents, health, jobs, search, traces


def create_app() -> FastAPI:
    app = FastAPI(title="RAG Knowledge Base", version="0.1.0")
    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(traces.router)
    app.include_router(chat.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)

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

    return app


app = create_app()
