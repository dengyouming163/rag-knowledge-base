"""不改 pipeline 的前提下采集 chat usage 与检索耗时。"""

from __future__ import annotations

import time
from typing import Any


class UsageRecordingChatClient:
    """旁路记录最近一次 chat 结果，供 trace 读取 token/耗时。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.last: Any = None

    def chat(self, *args, **kwargs):
        result = self.inner.chat(*args, **kwargs)
        self.last = result
        return result


class TimedRetriever:
    """只给 retrieve 计时，其余行为与内层检索器保持一致。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.last_ms: float = 0.0

    def retrieve(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self.inner.retrieve(*args, **kwargs)
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000


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
