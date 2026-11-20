"""检索接口：`POST /v1/kbs/{kb_id}/search`。

租户只从依赖注入取（请求头 → Phase 3 换成 token），请求体里出现 tenant_id 会 422。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response

from app.deps import EmbedderDep, EngineDep, TenantId, TraceStoreDep, UserId, user_context_of
from app.observability import TimedRetriever
from app.schemas import HitOut, SearchRequest, SearchResponse
from app.services import build_retriever, ensure_kb
from src.ops_observability import try_record_rag_metric
from src.trace import new_trace_id

router = APIRouter(prefix="/v1", tags=["search"])


@router.post("/kbs/{kb_id}/search", response_model=SearchResponse)
def search(
    kb_id: str,
    payload: SearchRequest,
    request: Request,
    response: Response,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
    embed_client: EmbedderDep,
    trace_store: TraceStoreDep,
):
    trace_id = new_trace_id()
    request.state.trace_id = trace_id
    request.state.kb_id = kb_id
    response.headers["X-Trace-Id"] = trace_id
    origin = time.perf_counter()
    ensure_kb(engine, tenant_id, kb_id)
    user_tags, is_admin = user_context_of(engine, tenant_id, user_id)

    started = time.perf_counter()
    timed: TimedRetriever | None = None
    try:
        retriever = build_retriever(
            engine,
            tenant_id,
            kb_id,
            embed_client,
            need_bm25=payload.mode in ("hybrid", "bm25"),
        )
        timed = TimedRetriever(retriever, origin=origin)
        hits = timed.retrieve(
            payload.query,
            top_k=payload.top_k,
            filters={"audience": payload.audience, "user_tags": user_tags, "is_admin": is_admin},
            mode=payload.mode,
        )
    except Exception as exc:
        finished = time.perf_counter()
        elapsed_ms = (finished - started) * 1000
        trace_store.record(
            trace_id=trace_id,
            tenant=tenant_id,
            kb=kb_id,
            stage="error",
            ms=elapsed_ms,
            hits=0,
            refused=False,
            mode=payload.mode,
            query=payload.query,
        )
        try_record_rag_metric(
            engine,
            trace_id=trace_id,
            tenant_id=tenant_id,
            kb_id=kb_id,
            operation="search",
            mode=payload.mode,
            outcome="error",
            hits=0,
            error_kind=exc.__class__.__name__,
            total_ms=(finished - origin) * 1000,
            retrieval_ms=timed.last_ms if timed is not None else None,
            stage_spans=(
                [timed.last_span]
                if timed is not None and timed.last_span is not None
                else []
            ),
        )
        raise
    finished = time.perf_counter()
    elapsed_ms = (finished - started) * 1000
    trace_store.record(
        trace_id=trace_id,
        tenant=tenant_id,
        kb=kb_id,
        stage="retrieve",
        ms=elapsed_ms,
        hits=len(hits),
        refused=False,
        mode=payload.mode,
        query=payload.query,
    )
    try_record_rag_metric(
        engine,
        trace_id=trace_id,
        tenant_id=tenant_id,
        kb_id=kb_id,
        operation="search",
        mode=payload.mode,
        outcome="succeeded",
        hits=len(hits),
        citation_ok=None,
        rerank_degraded=None,
        total_ms=(finished - origin) * 1000,
        retrieval_ms=timed.last_ms,
        stage_spans=[timed.last_span],
    )

    return SearchResponse(
        kb_id=kb_id,
        query=payload.query,
        mode=payload.mode,
        hits=[
            HitOut(
                chunk_id=hit.chunk_id,
                doc_id=hit.doc_id,
                score=round(float(hit.score), 6),
                text=hit.text,
                metadata=hit.metadata,
            )
            for hit in hits
        ],
    )
