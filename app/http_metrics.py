from __future__ import annotations

import asyncio
import logging
import time

from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import Headers

from src.ops_observability import try_record_http_metric

from .deps import get_engine

logger = logging.getLogger(__name__)


def _instrumentable(scope) -> bool:
    path = scope.get("path", "")
    if not path.startswith("/v1/") or path.startswith("/v1/ops/"):
        return False
    tenant_id = Headers(scope=scope).get("x-tenant-id")
    return bool(tenant_id and tenant_id.strip())


def _fields_from_scope(scope, status_code: int, duration_ms: float) -> dict:
    route = scope.get("route")
    path_params = scope.get("path_params") or {}
    state = scope.get("state") or {}
    return {
        "tenant_id": Headers(scope=scope)["x-tenant-id"].strip(),
        "kb_id": path_params.get("kb_id") or state.get("kb_id"),
        "route": getattr(route, "path", None) or "unmatched",
        "method": scope["method"],
        "status_code": status_code,
        "duration_ms": duration_ms,
        "trace_id": state.get("trace_id"),
    }


def _write(fields: dict) -> None:
    try_record_http_metric(get_engine(), **fields)


class HttpMetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _instrumentable(scope):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500
        try:

            async def observed_send(message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                await send(message)

            await self.app(scope, receive, observed_send)
        finally:
            try:
                fields = _fields_from_scope(
                    scope, status_code, (time.perf_counter() - started) * 1000
                )
                await asyncio.shield(run_in_threadpool(_write, fields))
            except Exception:  # noqa: BLE001
                logger.exception("failed to record HTTP metric")
