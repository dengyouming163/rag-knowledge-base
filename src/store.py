"""向量存储抽象与基于 numpy + JSON 的单机实现（归一化后点积即余弦）。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel, Field

from .chunk import Chunk

STORE_FILENAME = "vectors.json"
STORE_VERSION = 1


class SearchHit(BaseModel):
    """一条检索结果；metadata 保留 section_path、title、audience、source_url。"""

    chunk_id: str
    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class VectorStore(Protocol):
    """向量库接口，便于后续替换为 Qdrant / pgvector。"""

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int: ...

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[SearchHit]: ...

    def delete_by_doc(self, doc_id: str) -> int: ...

    def count(self) -> int: ...


def _normalize(vector: list[float]) -> np.ndarray:
    """L2 归一化；零向量原样返回（相似度按 0 处理）。"""
    array = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(array))
    if norm == 0.0 or not np.isfinite(norm):
        return array
    return array / norm


class LocalVectorStore:
    """单机向量库：内存索引 + JSON 持久化，写入时先写临时文件再原子替换。

    filters 对 metadata 做精确匹配（当前主要用于 audience 过滤）。
    """

    def __init__(self, path: str | Path) -> None:
        self._dir = Path(path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / STORE_FILENAME
        self._records: dict[str, dict[str, Any]] = {}
        self._dim: int | None = None
        self._load()

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """按 content_hash 去重写入，返回实际新增条数；重复块直接跳过。"""
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量必须一致")

        written = 0
        for chunk, vector in zip(chunks, vectors):
            if chunk.content_hash in self._records:
                continue
            array = _normalize(vector)
            if self._dim is None:
                self._dim = int(array.size)
            elif array.size != self._dim:
                raise ValueError(
                    f"向量维度不一致：库内为 {self._dim}，本次为 {array.size}"
                )
            self._records[chunk.content_hash] = {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "text": chunk.text,
                "content_hash": chunk.content_hash,
                "metadata": {
                    "section_path": list(chunk.section_path),
                    "title": chunk.title,
                    "audience": chunk.audience,
                    "source_url": chunk.source_url,
                },
                "vector": array,
            }
            written += 1

        if written:
            self._flush()
        return written

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        """余弦相似度检索；filters 中的键值需与 metadata 精确相等。"""
        if top_k < 1:
            raise ValueError("top_k 至少为 1")
        if not self._records:
            return []

        query = _normalize(query_vector)
        if self._dim is not None and query.size != self._dim:
            raise ValueError(
                f"查询向量维度不一致：库内为 {self._dim}，查询为 {query.size}"
            )

        records = [
            record
            for record in self._records.values()
            if self._matches(record["metadata"], filters)
        ]
        if not records:
            return []

        matrix = np.vstack([record["vector"] for record in records])
        scores = matrix @ query
        order = np.argsort(-scores, kind="stable")[:top_k]
        return [
            SearchHit(
                chunk_id=records[index]["chunk_id"],
                doc_id=records[index]["doc_id"],
                text=records[index]["text"],
                score=float(scores[index]),
                metadata=dict(records[index]["metadata"]),
            )
            for index in order
        ]

    def delete_by_doc(self, doc_id: str) -> int:
        """删除某文档的全部块，返回删除条数。"""
        hashes = [
            content_hash
            for content_hash, record in self._records.items()
            if record["doc_id"] == doc_id
        ]
        for content_hash in hashes:
            del self._records[content_hash]
        if hashes:
            self._flush()
        return len(hashes)

    def count(self) -> int:
        return len(self._records)

    @staticmethod
    def _matches(metadata: dict[str, Any], filters: dict | None) -> bool:
        if not filters:
            return True
        return all(metadata.get(key) == value for key, value in filters.items())

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            records = payload["records"]
            dimension = payload.get("dim")
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"向量库文件损坏：{self._path}") from exc

        self._dim = int(dimension) if dimension is not None else None
        for record in records:
            stored = {
                "chunk_id": record["chunk_id"],
                "doc_id": record["doc_id"],
                "text": record["text"],
                "content_hash": record["content_hash"],
                "metadata": dict(record["metadata"]),
                "vector": np.asarray(record["vector"], dtype=np.float64),
            }
            self._records[stored["content_hash"]] = stored

    def _flush(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "dim": self._dim,
            "records": [
                {
                    "chunk_id": record["chunk_id"],
                    "doc_id": record["doc_id"],
                    "text": record["text"],
                    "content_hash": record["content_hash"],
                    "metadata": record["metadata"],
                    "vector": record["vector"].tolist(),
                }
                for record in self._records.values()
            ],
        }

        fd, tmp_name = tempfile.mkstemp(
            dir=self._dir, prefix=".vectors-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
