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

from fastapi import APIRouter, Request
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
from app.observability import (
    TimedReranker,
    TimedRetriever,
    UsageRecordingChatClient,
    usage_from_chat_result,
)
from app.schemas import ChatRequest
from app.services import build_pipeline, ensure_kb
from src.ops_observability import try_record_rag_metric
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
    request: Request = None,
):
    trace_id = new_trace_id()
    if request is not None:
        request.state.trace_id = trace_id
        request.state.kb_id = payload.kb_id
    origin = time.perf_counter()
    # kb 由请求体指定，但归属必须在生成器外校验：错误直接走 HTTP 404
    ensure_kb(engine, tenant_id, payload.kb_id)
    user_tags, is_admin = user_context_of(engine, tenant_id, user_id)

    usage_client = UsageRecordingChatClient(chat_client, origin=origin)
    timed_reranker = (
        TimedReranker(reranker, origin=origin) if reranker is not None else None
    )
    pipeline = build_pipeline(
        engine, tenant_id, payload.kb_id, embed_client, usage_client, timed_reranker
    )
    # 只给检索计时：包一层代理后，pipeline 其余行为不变
    timed_retriever = None
    retriever = getattr(pipeline, "retriever", None)
    if retriever is not None:
        timed_retriever = TimedRetriever(retriever, origin=origin)
        pipeline.retriever = timed_retriever

    pipeline_result: dict | None = None
    chunks: list[str] = []
    refused = False

    def build_metric_fields(outcome: str, **overrides) -> dict:
        answer_calls = [call for call in usage_client.calls if call["stage"] == "answer"]
        answer_call = answer_calls[-1] if answer_calls else None
        rewrite_calls = [call for call in usage_client.calls if call["stage"] == "rewrite"]
        answer_result = answer_call["result"] if answer_call is not None else None
        answer_usage = (
            usage_from_chat_result(answer_result) if answer_result is not None else None
        )
        spans = [
            {
                "stage": call["stage"],
                "start_ms": call["start_ms"],
                "duration_ms": call["duration_ms"],
                "status": "ok" if call["result"] is not None else "error",
            }
            for call in usage_client.calls
        ]
        if timed_retriever is not None and timed_retriever.last_span is not None:
            spans.append(timed_retriever.last_span)
        if timed_reranker is not None and timed_reranker.last_span is not None:
            spans.append(timed_reranker.last_span)
        spans.sort(key=lambda span: span["start_ms"])

        fields = {
            "trace_id": trace_id,
            "tenant_id": tenant_id,
            "kb_id": payload.kb_id,
            "operation": "chat",
            "mode": "chat",
            "outcome": outcome,
            "hits": len(chunks),
            "citation_ok": (
                pipeline_result.get("citation_ok", True)
                if pipeline_result is not None and answer_result is not None
                else None
            ),
            "rerank_degraded": (
                pipeline_result.get("rerank_degraded", False)
                if pipeline_result is not None
                and timed_reranker is not None
                and timed_reranker.last_span is not None
                else None
            ),
            "total_ms": (time.perf_counter() - origin) * 1000,
            "rewrite_ms": (
                sum(call["duration_ms"] for call in rewrite_calls)
                if rewrite_calls
                else None
            ),
            "retrieval_ms": (
                timed_retriever.last_ms
                if timed_retriever is not None and timed_retriever.last_span is not None
                else None
            ),
            "rerank_ms": (
                timed_reranker.last_ms
                if timed_reranker is not None and timed_reranker.last_span is not None
                else None
            ),
            "llm_ms": answer_call["duration_ms"] if answer_call is not None else None,
            "model": answer_usage["model"] if answer_usage is not None else None,
            "prompt_tokens": (
                answer_usage["prompt_tokens"] if answer_usage is not None else None
            ),
            "completion_tokens": (
                answer_usage["completion_tokens"] if answer_usage is not None else None
            ),
            "error_kind": None,
            "stage_spans": spans,
        }
        fields.update(overrides)
        return fields

    metric_recorded = False

    async def finalize_metrics(outcome: str, **overrides) -> None:
        nonlocal metric_recorded
        if metric_recorded:
            return
        metric_recorded = True
        fields = build_metric_fields(outcome=outcome, **overrides)
        await asyncio.shield(
            run_in_threadpool(try_record_rag_metric, engine, **fields)
        )

    async def event_stream():
        nonlocal pipeline_result, chunks, refused
        started = time.perf_counter()

        async def record_stage(
            stage: str,
            *,
            hits: int,
            refused: bool,
            mode: str,
            usage: dict | None = None,
            flags: dict | None = None,
        ) -> None:
            extra = {}
            if usage:
                extra.update(
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    model=usage.get("model"),
                    llm_ms=usage.get("llm_ms"),
                )
            if flags:
                extra.update(
                    rerank_degraded=flags.get("rerank_degraded"),
                    citation_ok=flags.get("citation_ok"),
                )
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
                **extra,
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
                pipeline_result = result
            except Exception as exc:  # noqa: BLE001 - 任何失败都要显式告知前端
                await record_stage("error", hits=0, refused=False, mode="error")
                await finalize_metrics("error", error_kind=exc.__class__.__name__)
                message = str(exc)[:300] or exc.__class__.__name__
                yield f"event: error\ndata: {json.dumps({'message': message}, ensure_ascii=False)}\n\n"
                return

            chunks = result["chunks"]
            answer = result["answer"]
            refused = not chunks or "根据现有资料无法确定" in answer
            # 除输出防护拒答（GUARD_ANSWER）外，早退拒答不会调用生成：
            # last 里残留的是多轮改写的结果，不能当成 answer 的 usage/flags 写进 trace
            # （SSE 仍用零值字典保持字段稳定）
            chat_result = usage_client.last if chunks else None
            usage = usage_from_chat_result(chat_result)
            flags = (
                {
                    "rerank_degraded": result.get("rerank_degraded", False),
                    "citation_ok": result.get("citation_ok", True),
                }
                if chat_result is not None
                else None
            )
            timings = {
                "retrieval_ms": (
                    round(timed_retriever.last_ms, 3) if timed_retriever else 0.0
                ),
                "llm_ms": round(float(usage["llm_ms"] or 0), 3),
            }
            await record_stage(
                "answer",
                hits=len(chunks),
                refused=refused,
                mode="chat",
                usage=usage if chat_result is not None else None,
                flags=flags,
            )
            yield (
                "event: hits\ndata: "
                + json.dumps(
                    {
                        "chunks": chunks,
                        "rewritten": result["rewritten"],
                        "rerank_degraded": result.get("rerank_degraded", False),
                        "citation_ok": result.get("citation_ok", True),
                        "usage": usage,
                        "timings": timings,
                        "trace_id": trace_id,
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            for piece in _deltas(answer):
                yield _frame({"delta": piece})
            yield _frame("[DONE]")
            await finalize_metrics("refused" if refused else "succeeded")
        except asyncio.CancelledError:
            await record_cancelled()
            await finalize_metrics("cancelled")
            raise
        except GeneratorExit:
            await finalize_metrics("cancelled")
            raise

    headers = {**SSE_HEADERS, "X-Trace-Id": trace_id}
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)
