"""Rerank 适配层：精排是"锦上添花"，不是"生死线"。

为什么需要它：向量 + BM25 是双塔/词频式的粗排，快但不够准；cross-encoder 把
query 和候选拼在一起打分，准但慢。所以只对少量候选做，并且**必须有降级**——
精排服务挂掉时返回粗排结果，而不是让整个问答失败。

三个角色：
- RerankerPort：协议，便于替换服务商；
- ApiReranker：HTTP 调用，超时 800ms，失败自动降级；
- NullReranker：不排序，直接截断。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .store import SearchHit

DEFAULT_TIMEOUT = 0.8


@dataclass
class RerankResult:
    hits: list[SearchHit]
    degraded: bool = False
    reason: str | None = None


class RerankerPort(Protocol):
    def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> RerankResult: ...


class NullReranker:
    """降级实现：保持原顺序截断，绝不抛异常。"""

    def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> RerankResult:
        return RerankResult(hits=hits[:top_k])


class ApiReranker:
    """调用 /rerank 接口；任何失败都回退到 NullReranker 并标记 degraded。"""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        model: str = "bge-reranker-v2-m3",
        timeout: float = DEFAULT_TIMEOUT,
        client: Any | None = None,
        fallback: RerankerPort | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client = client
        self._fallback = fallback or NullReranker()

    def rerank(self, query: str, hits: list[SearchHit], top_k: int) -> RerankResult:
        if not hits:
            return RerankResult(hits=[])

        documents = [hit.text for hit in hits]
        payload = {"model": self._model, "query": query, "documents": documents, "top_n": top_k}

        try:
            response = self._post(payload)
            scores = _extract_scores(response, expected=len(documents))
        except Exception as exc:  # noqa: BLE001 - 精排失败一律降级，不影响主流程
            fallback = self._fallback.rerank(query, hits, top_k)
            return RerankResult(
                hits=fallback.hits, degraded=True, reason=f"{exc.__class__.__name__}: {exc}"[:200]
            )

        reranked = [
            hit.model_copy(update={"score": score, "metadata": {**hit.metadata, "rerank_score": score}})
            for hit, score in zip(hits, scores)
        ]
        reranked.sort(key=lambda item: (-item.score, item.chunk_id))
        return RerankResult(hits=reranked[:top_k])

    def _post(self, payload: dict) -> Any:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/rerank"

        if self._client is not None:
            response = self._client.post(url, json=payload, headers=headers, timeout=self._timeout)
        else:
            response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)

        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"rerank 返回 {response.status_code}")
        return response.json()


def _extract_scores(payload: dict, *, expected: int) -> list[float]:
    """把各家响应格式归一成"与输入等长的分数列表"。

    主流格式：{"results": [{"index": 0, "relevance_score": 0.9}, ...]}
    """
    rows = payload.get("results") or payload.get("data") or []
    if not isinstance(rows, list) or not rows:
        raise ValueError("rerank 响应缺少 results")

    scores = [0.0] * expected
    for row in rows:
        index = int(row.get("index", -1))
        if 0 <= index < expected:
            scores[index] = float(row.get("relevance_score", row.get("score", 0.0)))
    return scores


def build_reranker_from_env() -> RerankerPort:
    """没有配置 RERANK_BASE_URL 时不启用精排，避免无谓的网络调用。"""
    base_url = os.environ.get("RERANK_BASE_URL", "").strip()
    if not base_url:
        return NullReranker()
    return ApiReranker(
        base_url,
        api_key=os.environ.get("RERANK_API_KEY", ""),
        model=os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3"),
    )
