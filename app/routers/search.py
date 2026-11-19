"""检索接口：`POST /v1/kbs/{kb_id}/search`。

租户只从依赖注入取（请求头 → Phase 3 换成 token），请求体里出现 tenant_id 会 422。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import EmbedderDep, EngineDep, TenantId, UserId, user_context_of
from app.schemas import HitOut, SearchRequest, SearchResponse
from app.services import build_retriever, ensure_kb

router = APIRouter(prefix="/v1", tags=["search"])


@router.post("/kbs/{kb_id}/search", response_model=SearchResponse)
def search(
    kb_id: str,
    payload: SearchRequest,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
    embed_client: EmbedderDep,
):
    ensure_kb(engine, tenant_id, kb_id)
    user_tags, is_admin = user_context_of(engine, tenant_id, user_id)

    retriever = build_retriever(
        engine,
        tenant_id,
        kb_id,
        embed_client,
        need_bm25=payload.mode in ("hybrid", "bm25"),
    )
    hits = retriever.retrieve(
        payload.query,
        top_k=payload.top_k,
        filters={"audience": payload.audience, "user_tags": user_tags, "is_admin": is_admin},
        mode=payload.mode,
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
