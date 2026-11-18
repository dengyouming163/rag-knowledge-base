"""不依赖第三方库的 BM25 稀疏检索：中英文分词、倒排索引与 Okapi BM25 打分。"""

from __future__ import annotations

import math
import re
from collections import Counter

from .chunk import Chunk

_CJK_CLASS = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
_CJK_RUN_RE = re.compile(f"([{_CJK_CLASS}]+)")
_LATIN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """中文按单字 + 相邻双字（bigram），英文/数字按 \\w+ 切分并小写。

    中文没有空格边界，单字检索召回高但语义弱，双字 bigram 补足短语信息；
    不引入 jieba 等分词库，保持零依赖、行为可预测、便于单测。
    """
    tokens: list[str] = []
    for part in _CJK_RUN_RE.split(text):
        if not part:
            continue
        if _CJK_RUN_RE.fullmatch(part):
            chars = list(part)
            tokens.extend(chars)
            tokens.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
        else:
            tokens.extend(_LATIN_RE.findall(part.lower()))
    return tokens


class BM25Index:
    """内存倒排索引 + Okapi BM25（k1 控制词频饱和，b 控制文档长度归一化）。"""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        if k1 <= 0:
            raise ValueError("k1 必须为正数")
        if not 0 <= b <= 1:
            raise ValueError("b 必须落在 [0, 1] 区间")

        self.k1 = k1
        self.b = b
        self._chunks: dict[str, Chunk] = {chunk.chunk_id: chunk for chunk in chunks}
        self._freqs: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._inverted: dict[str, dict[str, int]] = {}

        for chunk in self._chunks.values():
            tokens = tokenize(chunk.text)
            freqs = Counter(tokens)
            self._freqs[chunk.chunk_id] = freqs
            self._lengths[chunk.chunk_id] = len(tokens)
            for term, count in freqs.items():
                self._inverted.setdefault(term, {})[chunk.chunk_id] = count

        total = len(self._freqs)
        self._avg_len = sum(self._lengths.values()) / total if total else 0.0

    def __len__(self) -> int:
        return len(self._chunks)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    def search(
        self, query: str, top_k: int = 10, filters: dict | None = None
    ) -> list[tuple[str, float]]:
        """返回 (chunk_id, score) 按分数降序、同分按 chunk_id 升序；空查询或无命中返回 []。"""
        if top_k < 1:
            raise ValueError("top_k 至少为 1")

        terms = tokenize(query)
        if not terms:
            return []

        total_docs = len(self._chunks)
        if total_docs == 0:
            return []

        scores: dict[str, float] = {}
        for term in dict.fromkeys(terms):
            postings = self._inverted.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            for chunk_id, tf in postings.items():
                chunk = self._chunks[chunk_id]
                if not self._matches(chunk, filters):
                    continue
                length = self._lengths[chunk_id]
                norm = 1 - self.b + self.b * (length / self._avg_len if self._avg_len else 1.0)
                scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * (
                    tf * (self.k1 + 1) / (tf + self.k1 * norm)
                )

        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:top_k]

    @staticmethod
    def _matches(chunk: Chunk, filters: dict | None) -> bool:
        if not filters:
            return True
        return all(getattr(chunk, key, None) == value for key, value in filters.items())
