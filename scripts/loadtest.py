#!/usr/bin/env python3
"""用有界 asyncio 并发测量 search 与 chat/stream，并如实保存全部请求样本的指标。

用法：
    py -3.13 scripts/loadtest.py --concurrency 20 --requests 200
    py -3.13 scripts/loadtest.py --base-url http://127.0.0.1:8000 --kb kb_after_sale \
        --tenant demo --query "退款多久到账" --out eval/loadtest_report.json

--requests 是每个端点各自的请求数，因此默认总共发送 400 个请求。
本脚本只测量并生成报告，不根据指标好坏调整并发或丢弃失败、慢请求。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "eval" / "loadtest_report.json"
RERANK_DENOMINATOR = "successful_chat_responses_with_valid_hits_event"
MAX_ERROR_SAMPLES = 20


class _RequestError(Exception):
    def __init__(self, kind: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def percentile(values: list[float], quantile: float) -> float | None:
    """排序后在 (n-1)*q 的位置做线性插值；空样本返回 None。"""
    if not 0 <= quantile <= 1:
        raise ValueError("quantile 必须在 [0, 1] 内")
    if not values:
        return None

    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_results(results: list[dict]) -> dict:
    """汇总端点结果；失败请求仍保留在延迟样本中。"""
    total = len(results)
    succeeded = sum(1 for result in results if result["success"])
    failed = total - succeeded
    latencies = [
        float(result["latency_ms"])
        for result in results
        if result.get("latency_ms") is not None
    ]
    failures = [result for result in results if not result["success"]]
    error_counts = Counter(result.get("error_type", "unknown") for result in failures)
    errors = [
        {
            "error_type": result.get("error_type", "unknown"),
            "error": result.get("error", ""),
            "status_code": result.get("status_code"),
            "latency_ms": result.get("latency_ms"),
        }
        for result in failures[:MAX_ERROR_SAMPLES]
    ]

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "error_rate": _ratio(failed, total),
        "latency_ms": {
            "sample_count": len(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "error_counts": dict(sorted(error_counts.items())),
        "errors": errors,
        "errors_truncated": max(0, failed - len(errors)),
    }


async def _search(
    client: httpx.AsyncClient,
    *,
    kb_id: str,
    query: str,
    top_k: int,
    mode: str,
    audience: str,
) -> None:
    response = await client.post(
        f"/v1/kbs/{kb_id}/search",
        json={"query": query, "top_k": top_k, "mode": mode, "audience": audience},
    )
    if not 200 <= response.status_code < 300:
        raise _RequestError(
            f"http_{response.status_code}",
            f"HTTP {response.status_code}: {response.text[:300]}",
            response.status_code,
        )
    try:
        response.json()
    except ValueError as exc:
        raise _RequestError("invalid_json", "search 成功响应不是有效 JSON") from exc


async def _sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """按 SSE 空行边界组帧，直到响应流完整结束。"""
    event = "message"
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines or event != "message":
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            field, value = line, ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines or event != "message":
        yield event, "\n".join(data_lines)


async def _chat(
    client: httpx.AsyncClient,
    *,
    kb_id: str,
    query: str,
    top_k: int,
    audience: str,
) -> bool:
    payload = {
        "kb_id": kb_id,
        "question": query,
        "history": [],
        "top_k": top_k,
        "audience": audience,
    }
    async with client.stream("POST", "/v1/chat/stream", json=payload) as response:
        if not 200 <= response.status_code < 300:
            await response.aread()
            raise _RequestError(
                f"http_{response.status_code}",
                f"HTTP {response.status_code}: {response.text[:300]}",
                response.status_code,
            )

        done = False
        hits_seen = False
        invalid_hits = False
        degraded = False
        sse_error: str | None = None
        async for event, data in _sse_events(response):
            if data == "[DONE]":
                done = True
            if event == "error":
                sse_error = data[:300] or "SSE error event"
            elif event == "hits":
                try:
                    hits = json.loads(data)
                    value = hits["rerank_degraded"]
                    if type(value) is not bool:
                        raise ValueError("rerank_degraded 不是 bool")
                except (KeyError, TypeError, ValueError):
                    invalid_hits = True
                else:
                    hits_seen = True
                    degraded = value

    # 即使已经看到 error，也已先消费到 EOF，再归类错误并计时。
    if sse_error is not None:
        raise _RequestError("sse_error", f"chat 返回 error 事件：{sse_error}")
    if not done:
        raise _RequestError("missing_done", "chat 流缺少 data: [DONE] 正常终止")
    if not hits_seen:
        kind = "invalid_hits" if invalid_hits else "missing_hits"
        raise _RequestError(kind, "chat 流缺少含布尔 rerank_degraded 的有效 hits 事件")
    return degraded


async def _attempt(operation: Callable[[], Awaitable[bool | None]]) -> dict:
    started = time.perf_counter()
    try:
        outcome = await operation()
    except Exception as exc:  # noqa: BLE001 - 单个失败必须进入报告，不能中止整轮压测
        result = {
            "success": False,
            "error_type": exc.kind if isinstance(exc, _RequestError) else type(exc).__name__,
            "error": str(exc)[:500] or type(exc).__name__,
        }
        if isinstance(exc, _RequestError) and exc.status_code is not None:
            result["status_code"] = exc.status_code
    else:
        result = {"success": True}
        if outcome is not None:
            result["rerank_degraded"] = outcome
    result["latency_ms"] = (time.perf_counter() - started) * 1000
    return result


async def run_loadtest(
    *,
    base_url: str,
    kb_id: str,
    tenant_id: str = "demo",
    query: str = "退款多久到账",
    concurrency: int = 20,
    requests: int = 200,
    top_k: int = 5,
    mode: str = "hybrid",
    audience: str = "public",
    user_id: str | None = None,
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """每个端点发送 requests 个请求，整个运行共用 concurrency 个 worker。"""
    if concurrency < 1 or requests < 1:
        raise ValueError("concurrency 与 requests 必须为正整数")
    if not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间（同时满足 chat 契约）")
    if timeout <= 0:
        raise ValueError("timeout 必须大于 0")

    timestamp = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    results: dict[str, list[dict]] = {"search": [], "chat": []}
    jobs = iter(["search"] * requests + ["chat"] * requests)
    headers = {"X-Tenant-Id": tenant_id}
    if user_id:
        headers["X-User-Id"] = user_id
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )

    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        limits=limits,
        transport=transport,
    ) as client:

        async def worker() -> None:
            for endpoint in jobs:
                if endpoint == "search":

                    async def operation() -> None:
                        await _search(
                            client,
                            kb_id=kb_id,
                            query=query,
                            top_k=top_k,
                            mode=mode,
                            audience=audience,
                        )

                else:

                    async def operation() -> bool:
                        return await _chat(
                            client,
                            kb_id=kb_id,
                            query=query,
                            top_k=top_k,
                            audience=audience,
                        )

                results[endpoint].append(await _attempt(operation))

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(concurrency, requests * 2))
        ]
        await asyncio.gather(*workers)

    search = summarize_results(results["search"])
    chat = summarize_results(results["chat"])
    rerank_observed = sum(1 for result in results["chat"] if result["success"])
    rerank_degraded = sum(
        1
        for result in results["chat"]
        if result["success"] and result["rerank_degraded"]
    )
    chat.update(
        {
            "rerank_degraded": rerank_degraded,
            "rerank_observed": rerank_observed,
            "rerank_degradation_rate": _ratio(rerank_degraded, rerank_observed),
            "rerank_denominator": RERANK_DENOMINATOR,
        }
    )

    return {
        "timestamp": timestamp,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "config": {
            "base_url": base_url.rstrip("/"),
            "kb_id": kb_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "query": query,
            "concurrency": concurrency,
            "requests_per_endpoint": requests,
            "total_requests": requests * 2,
            "top_k": top_k,
            "mode": mode,
            "audience": audience,
            "timeout_seconds": timeout,
        },
        "metric_definitions": {
            "latency_ms": "从请求开始到完整响应或 SSE 流结束；成功和失败请求均计入",
            "error_rate": "failed / total；total 为 0 时为 null",
            "percentile": "升序样本在 (n-1)*q 位置线性插值；空样本为 null",
            "rerank_degradation_rate": (
                "rerank_degraded / rerank_observed；分母仅含成功且有有效 hits 事件的 "
                "chat 响应，search 与失败 chat 均不参与"
            ),
        },
        "endpoints": {"search": search, "chat": chat},
    }


def write_report(path: Path, report: dict) -> None:
    """在目标目录写临时文件后原子替换，避免留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG search/chat 有界并发压测")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"),
        help="API 地址，也可用 API_BASE_URL 环境变量指定",
    )
    parser.add_argument("--kb", default="kb_after_sale", help="知识库 ID")
    parser.add_argument("--tenant", default="demo", help="X-Tenant-Id 请求头")
    parser.add_argument("--user-id", default=None, help="可选 X-User-Id 请求头")
    parser.add_argument("--query", default="退款多久到账", help="search query / chat question")
    parser.add_argument("--concurrency", type=_positive_int, default=20, help="全局 worker 上限")
    parser.add_argument(
        "--requests",
        type=_positive_int,
        default=200,
        help="每个端点分别发送的请求数；总请求数为此值的 2 倍",
    )
    parser.add_argument("--top-k", type=int, default=5, help="检索条数（1-20）")
    parser.add_argument("--mode", choices=("hybrid", "vector", "bm25"), default="hybrid")
    parser.add_argument("--audience", choices=("public", "internal"), default="public")
    parser.add_argument("--timeout", type=_positive_float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSON 报告输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.top_k <= 20:
        parser.error("--top-k 必须在 1 到 20 之间")

    report = asyncio.run(
        run_loadtest(
            base_url=args.base_url.rstrip("/"),
            kb_id=args.kb,
            tenant_id=args.tenant,
            query=args.query,
            concurrency=args.concurrency,
            requests=args.requests,
            top_k=args.top_k,
            mode=args.mode,
            audience=args.audience,
            user_id=args.user_id,
            timeout=args.timeout,
        )
    )
    write_report(args.out, report)
    search = report["endpoints"]["search"]
    chat = report["endpoints"]["chat"]

    def format_rate(value: float | None) -> str:
        return "null" if value is None else f"{value:.2%}"

    print(
        f"完成：每端点 {args.requests} 请求，并发 {args.concurrency}；"
        f"search 错误率={format_rate(search['error_rate'])}，"
        f"chat 错误率={format_rate(chat['error_rate'])}，"
        f"rerank 降级率={format_rate(chat.get('rerank_degradation_rate'))}"
    )
    print(f"报告：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
