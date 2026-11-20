"""不改 pipeline 的前提下采集 chat usage 与检索、精排耗时。"""

from __future__ import annotations

import time
from typing import Any


class UsageRecordingChatClient:
    """旁路记录每次 chat 结果与耗时，并保留最近一次成功结果。"""

    def __init__(self, inner: Any, *, origin: float | None = None) -> None:
        self.inner = inner
        self.origin = origin
        self.last: Any = None
        self.calls: list[dict] = []

    def chat(self, *args, **kwargs):
        started = time.perf_counter()
        result = None
        try:
            result = self.inner.chat(*args, **kwargs)
            self.last = result
            return result
        finally:
            finished = time.perf_counter()
            self.calls.append(
                {
                    "result": result,
                    "start_ms": (
                        0.0 if self.origin is None else (started - self.origin) * 1000
                    ),
                    "duration_ms": (finished - started) * 1000,
                }
            )


class TimedRetriever:
    """只给 retrieve 计时，其余行为与内层检索器保持一致。"""

    def __init__(self, inner: Any, *, origin: float | None = None) -> None:
        self.inner = inner
        self.origin = origin
        self.last_ms: float = 0.0
        self.last_span = None

    def retrieve(self, *args, **kwargs):
        started = time.perf_counter()
        status = "ok"
        try:
            return self.inner.retrieve(*args, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            finished = time.perf_counter()
            self.last_ms = (finished - started) * 1000
            self.last_span = {
                "stage": "retrieve",
                "start_ms": 0.0 if self.origin is None else (started - self.origin) * 1000,
                "duration_ms": self.last_ms,
                "status": status,
            }


class TimedReranker:
    """透明代理精排调用，记录最近一次真实执行的阶段跨度。"""

    def __init__(self, inner: Any, *, origin: float | None = None) -> None:
        self.inner = inner
        self.origin = origin
        self.last_ms: float | None = None
        self.last_span = None

    def rerank(self, *args, **kwargs):
        started = time.perf_counter()
        status = "ok"
        try:
            return self.inner.rerank(*args, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            finished = time.perf_counter()
            self.last_ms = (finished - started) * 1000
            self.last_span = {
                "stage": "rerank",
                "start_ms": 0.0 if self.origin is None else (started - self.origin) * 1000,
                "duration_ms": self.last_ms,
                "status": status,
            }


def usage_from_chat_result(result: Any) -> dict:
    """缺失字段（含拒答未调用模型时的 None）统一按 0 处理，不抛异常。"""
    prompt = int(getattr(result, "prompt_tokens", 0) or 0)
    completion = int(getattr(result, "completion_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "model": getattr(result, "model", "") or "",
        "llm_ms": float(getattr(result, "latency_ms", 0.0) or 0.0),
    }
