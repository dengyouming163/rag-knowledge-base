"""SSE 对话接口：`POST /v1/chat/stream`。

帧格式（与主流实现一致）：
    event: hits
    data: {"chunks": ["kb-after-sale#1"], "rewritten": "..."}

    data: {"delta": "退款"}
    data: {"delta": "通常 1-3 个工作日"}
    data: [DONE]

    event: error
    data: {"message": "..."}

说明：ChatClient 目前是同步一次性返回，这里按片段下发，协议保持不变；
后续接入真正的流式模型时只需要替换 `_deltas` 的来源，前端无需改动。
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from app.deps import (
    ChatClientDep,
    EmbedderDep,
    EngineDep,
    RerankerDep,
    TenantId,
    TraceStoreDep,
    UserId,
    user_context_of,
)
from app.schemas import ChatRequest
from app.services import build_pipeline, ensure_kb
from src.trace import new_trace_id

router = APIRouter(prefix="/v1", tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx 默认会缓冲响应，不加这行 SSE 会被攒成一坨再返回
    "X-Accel-Buffering": "no",
}

DELTA_SIZE = 12


def _deltas(text: str) -> list[str]:
    return [text[index : index + DELTA_SIZE] for index in range(0, len(text), DELTA_SIZE)]


def _frame(data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    tenant_id: TenantId,
    engine: EngineDep,
    embed_client: EmbedderDep,
    chat_client: ChatClientDep,
    reranker: RerankerDep,
    user_id: UserId,
    trace_store: TraceStoreDep,
):
    trace_id = new_trace_id()
    # kb 由请求体指定，但归属必须在生成器外校验：错误直接走 HTTP 404
    ensure_kb(engine, tenant_id, payload.kb_id)
    user_tags, is_admin = user_context_of(engine, tenant_id, user_id)

    pipeline = build_pipeline(
        engine, tenant_id, payload.kb_id, embed_client, chat_client, reranker
    )

    async def event_stream():
        started = time.perf_counter()

        async def record_stage(stage: str, *, hits: int, refused: bool, mode: str) -> None:
            await run_in_threadpool(
                trace_store.record,
                trace_id=trace_id,
                tenant=tenant_id,
                kb=payload.kb_id,
                stage=stage,
                ms=(time.perf_counter() - started) * 1000,
                hits=hits,
                refused=refused,
                mode=mode,
                query=payload.question,
            )

        async def record_cancelled() -> None:
            task = asyncio.create_task(
                record_stage("cancelled", hits=0, refused=False, mode="error")
            )
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # 重复取消不再等待，但 shield 内的线程池记录仍会继续完成。
                pass

        try:
            try:
                result = await run_in_threadpool(
                    pipeline.answer,
                    payload.question,
                    payload.history,
                    payload.audience,
                    payload.top_k,
                    user_tags,
                    is_admin,
                )
            except Exception as exc:  # noqa: BLE001 - 任何失败都要显式告知前端
                await record_stage("error", hits=0, refused=False, mode="error")
                message = str(exc)[:300] or exc.__class__.__name__
                yield f"event: error\ndata: {json.dumps({'message': message}, ensure_ascii=False)}\n\n"
                return

            chunks = result["chunks"]
            answer = result["answer"]
            refused = not chunks or "根据现有资料无法确定" in answer
            await record_stage("answer", hits=len(chunks), refused=refused, mode="chat")
            yield (
                "event: hits\ndata: "
                + json.dumps(
                    {
                        "chunks": chunks,
                        "rewritten": result["rewritten"],
                        "rerank_degraded": result.get("rerank_degraded", False),
                        "citation_ok": result.get("citation_ok", True),
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            for piece in _deltas(answer):
                yield _frame({"delta": piece})
            yield _frame("[DONE]")
        except asyncio.CancelledError:
            await record_cancelled()
            raise

    headers = {**SSE_HEADERS, "X-Trace-Id": trace_id}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
