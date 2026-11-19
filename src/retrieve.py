"""混合检索：向量召回 + BM25 召回，用 RRF（Reciprocal Rank Fusion）融合排名。"""

from __future__ import annotations

from typing import Literal, Protocol

from .acl import is_visible
from .bm25 import BM25Index
from .chunk import Chunk
from .route import DEFAULT_ROUTE, Route, detect_route
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
        route: Route | None = None,
    ) -> list[SearchHit]:
        """两路各取 recall_k 条；hybrid 用加权 RRF 融合后取 top_k，单路模式返回原始分数。"""
        if mode not in MODES:
            raise ValueError(f"mode 只能是 {MODES}")
        if top_k < 1 or recall_k < 1:
            raise ValueError("top_k 与 recall_k 至少为 1")

        if mode == "vector":
            return self._vector_route(query, recall_k, filters)[:top_k]
        if mode == "bm25":
            return self._bm25_route(query, recall_k, filters)[:top_k]

        # 没有显式指定时按规则自动路由：含型号/错误码的问题偏向 BM25
        active_route = route or detect_route(query)
        vector_hits = self._vector_route(query, recall_k, filters)
        bm25_hits = self._bm25_route(query, recall_k, filters)
        return self._fuse(
            vector_hits,
            bm25_hits,
            weights=(active_route.vector_weight, active_route.bm25_weight),
            route_name=active_route.name,
        )[:top_k]

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
        # user_tags 是 ACL 语义，不是文档元数据，不能交给 BM25 的精确匹配
        user_tags = list((filters or {}).get("user_tags") or [])
        bm25_filters = {k: v for k, v in (filters or {}).items() if k != "user_tags"} or None

        for chunk_id, score in self._bm25.search(query, top_k=recall_k, filters=bm25_filters):
            chunk = self._bm25.get_chunk(chunk_id)
            if chunk is None:
                continue
            if not is_visible(user_tags, chunk.acl_tags):
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
    def _fuse(
        *routes: list[SearchHit],
        weights: tuple[float, ...] | None = None,
        route_name: str = DEFAULT_ROUTE.name,
    ) -> list[SearchHit]:
        """score = Σ weight_i / (RRF_K + rank_i)，rank 从 1 起；同分按 chunk_id 升序。

        仍然只用名次：权重只放大某一路的名次贡献，不让原始分数的量纲进来。
        """
        if weights is None:
            # 未指定权重时均分：与 DEFAULT_ROUTE 的 0.5/0.5 语义一致
            weights = tuple(1.0 / len(routes) for _ in routes)
        if len(weights) != len(routes):
            raise ValueError("weights 数量必须与召回路数一致")

        scores: dict[str, float] = {}
        best: dict[str, SearchHit] = {}
        for weight, hits in zip(weights, routes):
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = (
                    scores.get(hit.chunk_id, 0.0) + weight / (RRF_K + rank)
                )
                # 先到先得：向量库的 metadata 优先，BM25 独有条目用索引里的元数据补全
                best.setdefault(hit.chunk_id, hit)

        fused = [
            SearchHit(
                chunk_id=chunk_id,
                doc_id=best[chunk_id].doc_id,
                text=best[chunk_id].text,
                score=score,
                metadata={**best[chunk_id].metadata, "route": route_name},
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
        "acl_tags": list(chunk.acl_tags),
    }
