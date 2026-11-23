"""基于 SiliconFlow（OpenAI 兼容）的 Embedding 客户端，带分批、指数退避重试与用量统计。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_BATCH_SIZE = 16
DEFAULT_MAX_RETRIES = 3


class EmbeddingError(Exception):
    """Embedding 调用失败，携带 HTTP 状态码（网络/配置错误时为空）。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def __str__(self) -> str:
        if self.status_code is None:
            return self.message
        return f"[HTTP {self.status_code}] {self.message}"


class EmbeddingClient:
    """调用 OpenAI 兼容的 /embeddings 接口。

    配置优先取显式参数，其次取环境变量 EMBED_API_KEY / EMBED_BASE_URL / EMBED_MODEL。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_backoff: float = 0.5,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or os.environ.get("EMBED_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("EMBED_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get("EMBED_MODEL") or DEFAULT_MODEL

        missing = []
        if not self.api_key:
            missing.append("EMBED_API_KEY")
        if not self.model:
            missing.append("EMBED_MODEL")
        if missing:
            raise EmbeddingError(
                "缺少必要配置：" + "、".join(missing) + "。请参考 .env.example 设置环境变量。"
            )

        if max_retries < 1:
            raise ValueError("max_retries 至少为 1")
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.timeout = timeout
        self._sleep = sleep
        self._endpoint = f"{self.base_url}/embeddings"
        self._client = httpx.Client(timeout=timeout)

        self._calls = 0
        self._total_tokens = 0
        self._total_texts = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EmbeddingClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def stats(self) -> dict[str, int]:
        """累计用量：HTTP 成功调用次数、token 总量、已向量化文本数。"""
        return {
            "calls": self._calls,
            "total_tokens": self._total_tokens,
            "total_texts": self._total_texts,
        }

    def embed_texts(
        self, texts: list[str], batch_size: int = DEFAULT_BATCH_SIZE
    ) -> list[list[float]]:
        """分批向量化，返回与输入顺序一致的结果；同批次维度必须一致。"""
        if batch_size < 1:
            raise ValueError("batch_size 至少为 1")
        if not texts:
            return []

        vectors: list[list[float]] = []
        dimension: int | None = None
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            batch_vectors = self._embed_batch(batch)
            for vector in batch_vectors:
                if dimension is None:
                    dimension = len(vector)
                elif len(vector) != dimension:
                    raise EmbeddingError(
                        f"向量维度不一致：期望 {dimension}，实际 {len(vector)}（model={self.model}）"
                    )
            vectors.extend(batch_vectors)
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """发送单批请求；仅对超时、连接错误、429、5xx 做指数退避重试。"""
        payload = {"model": self.model, "input": texts}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.post(
                    self._endpoint, json=payload, headers=headers, timeout=self.timeout
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_retries:
                    raise EmbeddingError(
                        f"请求失败（{type(exc).__name__}），已重试 {self.max_retries} 次"
                    ) from exc
                self._backoff(attempt)
                continue

            status = response.status_code

            if status == 429 or status >= 500:
                if attempt == self.max_retries:
                    raise EmbeddingError(
                        f"服务端错误，已重试 {self.max_retries} 次：{response.text}",
                        status_code=status,
                    )
                self._backoff(attempt)
                continue

            if status >= 400:
                raise EmbeddingError(
                    f"请求被拒绝：{response.text}", status_code=status
                )

            return self._parse_response(response, len(texts))

        raise EmbeddingError("请求失败：重试逻辑异常终止")

    def _backoff(self, attempt: int) -> None:
        self._sleep(self.base_backoff * (2 ** (attempt - 1)))

    def _parse_response(
        self, response: httpx.Response, expected: int
    ) -> list[list[float]]:
        try:
            data = response.json()
            items = data["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("响应格式异常，无法解析 data 字段") from exc

        if not isinstance(items, list):
            raise EmbeddingError("响应格式异常：data 不是数组")
        if len(items) != expected:
            raise EmbeddingError(
                f"响应条数与请求不一致：期望 {expected}，实际 {len(items)}"
            )

        # 服务端可能乱序返回，按 index 还原输入顺序
        if all(isinstance(item, dict) and "index" in item for item in items):
            items = sorted(items, key=lambda item: item["index"])

        vectors: list[list[float]] = []
        try:
            for item in items:
                vector = [float(value) for value in item["embedding"]]
                if not vector:
                    raise EmbeddingError("响应包含空向量")
                vectors.append(vector)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError("响应格式异常，无法解析 data[].embedding") from exc

        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
        self._calls += 1
        self._total_tokens += int(tokens)
        self._total_texts += expected
        return vectors
