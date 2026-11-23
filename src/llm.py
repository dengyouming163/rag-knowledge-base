"""基于 httpx 的 LLM Chat 客户端，带指数退避重试与用量统计。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_MAX_RETRIES = 3
RETRYABLE_STATUS_CODES = {429}


class LLMError(Exception):
    """LLM 调用失败，携带 HTTP 状态码（网络/配置错误时为空）。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"[HTTP {self.status_code}] {self.message}"


class ChatResult(BaseModel):
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float
    model: str


class ChatClient:
    """调用 OpenAI 兼容的 /chat/completions 接口。

    配置优先取显式参数，其次取环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL

        missing = []
        if not self.api_key:
            missing.append("LLM_API_KEY")
        if not self.model:
            missing.append("LLM_MODEL")
        if missing:
            raise LLMError(
                "缺少必要配置：" + "、".join(missing) + "。请参考 .env.example 设置环境变量。"
            )

        if max_retries < 1:
            raise ValueError("max_retries 至少为 1")
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self._sleep = sleep
        self._endpoint = f"{self.base_url}/chat/completions"
        self._client = httpx.Client(timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ChatClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 512,
        timeout: float = 30.0,
    ) -> ChatResult:
        """发送对话请求；仅对超时、连接错误、429、5xx 做指数退避重试。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.post(
                    self._endpoint, json=payload, headers=headers, timeout=timeout
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise LLMError(
                        f"请求失败（{type(exc).__name__}），已重试 {self.max_retries} 次"
                    ) from exc
                self._backoff(attempt)
                continue

            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            status = response.status_code

            if status == 429 or status >= 500:
                if attempt == self.max_retries:
                    raise LLMError(
                        f"服务端错误，已重试 {self.max_retries} 次：{response.text}",
                        status_code=status,
                    )
                self._backoff(attempt)
                continue

            if status >= 400:
                raise LLMError(f"请求被拒绝：{response.text}", status_code=status)

            return self._parse_response(response, latency_ms)

        raise LLMError("请求失败：重试逻辑异常终止")

    def _backoff(self, attempt: int) -> None:
        self._sleep(self.base_backoff * (2 ** (attempt - 1)))

    def _parse_response(self, response: httpx.Response, latency_ms: float) -> ChatResult:
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("响应格式异常，无法解析 choices[0].message.content") from exc

        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            latency_ms=latency_ms,
            model=data.get("model") or self.model,
        )
