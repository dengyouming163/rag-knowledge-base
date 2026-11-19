"""规则路由：先判断问题长得像什么，再决定两路召回的权重。

为什么需要：型号、错误码、条款编号这类"强标识符"，向量模型往往不敏感——
它会把 `X200-Pro` 当成普通语义片段，而 BM25 能精确命中这个词。所以含标识符的
问题提高 BM25 权重，其余问题维持均权。

只做规则、不调 LLM：路由在每次查询的关键路径上，加一次模型调用会让 P99 翻倍。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 型号/错误码/编号：X200、AB12、E1024、SKU-1001
IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z]{1,5}\d{2,}"),
    re.compile(r"\bE\d{4}\b"),
    re.compile(r"\b[A-Z]{2,}-\d+\b"),
)


@dataclass(frozen=True)
class Route:
    """融合权重：两路权重之和为 1，便于解释。"""

    name: str
    vector_weight: float
    bm25_weight: float


DEFAULT_ROUTE = Route(name="default", vector_weight=0.5, bm25_weight=0.5)
IDENTIFIER_ROUTE = Route(name="identifier", vector_weight=0.3, bm25_weight=0.7)


def detect_route(query: str) -> Route:
    if not query or not query.strip():
        return DEFAULT_ROUTE
    if any(pattern.search(query) for pattern in IDENTIFIER_PATTERNS):
        return IDENTIFIER_ROUTE
    return DEFAULT_ROUTE
