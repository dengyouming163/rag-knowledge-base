"""混合检索：向量召回 + BM25 召回，用 RRF（Reciprocal Rank Fusion）融合排名。"""

from __future__ import annotations

from typing import Literal, Protocol

from .bm25 import BM25Index
from .chunk import Chunk
from .store import SearchHit, VectorStore

RRF_K = 60

Mode = Literal["hybrid", "vector", "bm25"]
MODES: tuple[Mode, ...] = ("vector", "bm25", "hybrid")


class EmbedClient(Protocol):
    """只需要单批向量化能力，便于测试注入假实现。"""

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class HybridRetriever:
    """两路召回 + RRF 融合。

    RRF 只用名次不用原始分数：向量余弦与 BM25 分数量纲不同，直接加权求和需要
    额外调参；取 1/(k+rank) 后两路天然可比，且对单路的异常高分不敏感。
    """

    def __init__(
        self,
        vector_store: VectorStore,
        bm25_index: BM25Index,
        embed_client: EmbedClient,
    ) -> None:
        self._vectors = vector_store
        self._bm25 = bm25_index
        self._embed = embed_client

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        recall_k: int = 40,
        filters: dict | None = None,
        mode: Mode = "hybrid",
    ) -> list[SearchHit]:
        """两路各取 recall_k 条；hybrid 用 RRF 融合后取 top_k，单路模式返回原始分数。"""
        if mode not in MODES:
            raise ValueError(f"mode 只能是 {MODES}")
        if top_k < 1 or recall_k < 1:
            raise ValueError("top_k 与 recall_k 至少为 1")

        if mode == "vector":
            return self._vector_route(query, recall_k, filters)[:top_k]
        if mode == "bm25":
            return self._bm25_route(query, recall_k, filters)[:top_k]

        vector_hits = self._vector_route(query, recall_k, filters)
        bm25_hits = self._bm25_route(query, recall_k, filters)
        return self._fuse(vector_hits, bm25_hits)[:top_k]

    def compare_routes(
        self,
        query: str,
        top_k: int = 5,
        recall_k: int = 40,
        filters: dict | None = None,
    ) -> dict[str, list[tuple[str, float]]]:
        """三路对比：每路返回 top_k 的 (chunk_id, score)，供对比脚本与报告使用。"""
        return {
            mode: [
                (hit.chunk_id, hit.score)
                for hit in self.retrieve(
                    query, top_k=top_k, recall_k=recall_k, filters=filters, mode=mode
                )
            ]
            for mode in MODES
        }

    def _vector_route(
        self, query: str, recall_k: int, filters: dict | None
    ) -> list[SearchHit]:
        vectors = self._embed.embed_texts([query])
        if len(vectors) != 1:
            raise ValueError("embed_client 必须为单条查询返回一个向量")
        return self._vectors.search(vectors[0], top_k=recall_k, filters=filters)

    def _bm25_route(
        self, query: str, recall_k: int, filters: dict | None
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for chunk_id, score in self._bm25.search(query, top_k=recall_k, filters=filters):
            chunk = self._bm25.get_chunk(chunk_id)
            if chunk is None:
                continue
            hits.append(
                SearchHit(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    text=chunk.text,
                    score=score,
                    metadata=_metadata_of(chunk),
                )
            )
        return hits

    @staticmethod
    def _fuse(*routes: list[SearchHit]) -> list[SearchHit]:
        """score = Σ 1/(RRF_K + rank)，rank 从 1 起；同分按 chunk_id 升序。"""
        scores: dict[str, float] = {}
        best: dict[str, SearchHit] = {}
        for hits in routes:
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank)
                # 先到先得：向量库的 metadata 优先，BM25 独有条目用索引里的元数据补全
                best.setdefault(hit.chunk_id, hit)

        fused = [
            SearchHit(
                chunk_id=chunk_id,
                doc_id=best[chunk_id].doc_id,
                text=best[chunk_id].text,
                score=score,
                metadata=dict(best[chunk_id].metadata),
            )
            for chunk_id, score in scores.items()
        ]
        fused.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return fused


def _metadata_of(chunk: Chunk) -> dict:
    return {
        "section_path": list(chunk.section_path),
        "title": chunk.title,
        "audience": chunk.audience,
        "source_url": chunk.source_url,
    }
