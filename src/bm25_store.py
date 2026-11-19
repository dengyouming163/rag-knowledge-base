"""BM25 倒排索引的持久化与进程内 LRU 缓存。

为什么需要它：进程重启后，内存里的 BM25 索引就没了；如果没有快照，
每次冷启动都要把整库几万条文本重新分词一遍。快照把"分词"这个最贵的一步固化下来。

设计要点：
1. 一个知识库一份快照，`kb_bm25` 表按 kb_id 唯一；
2. `version` 单调递增，缓存命中前先比对版本，避免读到旧索引；
3. LRU 只缓存最近用过的 N 个库，防止多租户场景把内存吃满。
"""

from __future__ import annotations

import json
from collections import OrderedDict

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .bm25 import BM25Index
from .chunk import Chunk
from .db.session import tenant_scope


class IndexNotBuiltError(LookupError):
    """该知识库还没有构建过 BM25 快照。"""


class BM25Store:
    def __init__(self, engine: Engine, cache_size: int = 8) -> None:
        self._engine = engine
        self._cache_size = max(1, cache_size)
        self._cache: OrderedDict[str, tuple[int, BM25Index]] = OrderedDict()

    # ---------------------------------------------------------------- 构建

    def rebuild(self, tenant_id: str, kb_id: str) -> int:
        """从 chunks 表重建该库的倒排索引并写回快照，返回新版本号。"""
        chunks = self._load_chunks(tenant_id, kb_id)
        if not chunks:
            raise ValueError(f"知识库 {kb_id} 没有可用的 chunk，无法构建 BM25 索引")

        payload = BM25Index(chunks).to_payload()

        with self._engine.begin() as connection:
            version = connection.execute(
                text(
                    "INSERT INTO kb_bm25 (kb_id, tenant_id, version, payload, updated_at) "
                    "VALUES (:kb_id, :tenant_id, 1, CAST(:payload AS jsonb), now()) "
                    "ON CONFLICT (kb_id) DO UPDATE SET "
                    "  version = kb_bm25.version + 1, "
                    "  tenant_id = EXCLUDED.tenant_id, "
                    "  payload = EXCLUDED.payload, "
                    "  updated_at = now() "
                    "RETURNING version"
                ),
                {
                    "kb_id": kb_id,
                    "tenant_id": tenant_id,
                    "payload": json.dumps(payload, ensure_ascii=False),
                },
            ).scalar_one()

        # 重建后立刻让旧缓存失效，否则下一次 load 还会拿到旧版本
        self._cache.pop(kb_id, None)
        return int(version)

    def _load_chunks(self, tenant_id: str, kb_id: str) -> list[Chunk]:
        with tenant_scope(self._engine, tenant_id) as connection:
            rows = connection.execute(
                text(
                    "SELECT chunk_id, doc_id, text, content_hash, token_count, meta "
                    "FROM chunks "
                    "WHERE tenant_id = :tenant_id AND kb_id = :kb_id AND is_parent = false "
                    "ORDER BY chunk_id"
                ),
                {"tenant_id": tenant_id, "kb_id": kb_id},
            ).mappings().all()

        return [
            Chunk(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                text=row["text"],
                title=(row["meta"] or {}).get("title", ""),
                section_path=(row["meta"] or {}).get("section_path", []),
                source_url=(row["meta"] or {}).get("source_url", ""),
                content_hash=row["content_hash"],
                audience=(row["meta"] or {}).get("audience", "public"),
                token_count=row["token_count"],
                acl_tags=list((row["meta"] or {}).get("acl_tags") or []),
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- 读取

    def version(self, kb_id: str) -> int | None:
        with self._engine.connect() as connection:
            return connection.execute(
                text("SELECT version FROM kb_bm25 WHERE kb_id = :kb_id"), {"kb_id": kb_id}
            ).scalar_one_or_none()

    def load(self, kb_id: str) -> BM25Index:
        """返回索引；命中 LRU 且版本一致时直接复用，否则从快照还原。"""
        with self._engine.connect() as connection:
            row = connection.execute(
                text("SELECT version, payload FROM kb_bm25 WHERE kb_id = :kb_id"),
                {"kb_id": kb_id},
            ).mappings().first()

        if row is None:
            raise IndexNotBuiltError(f"知识库 {kb_id} 尚未构建 BM25 索引")

        version = int(row["version"])
        cached = self._cache.get(kb_id)
        if cached is not None and cached[0] == version:
            self._cache.move_to_end(kb_id)
            return cached[1]

        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else raw
        index = BM25Index.from_payload(payload)

        self._cache[kb_id] = (version, index)
        self._cache.move_to_end(kb_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return index

    def invalidate(self, kb_id: str) -> None:
        self._cache.pop(kb_id, None)

    @property
    def cached_kbs(self) -> list[str]:
        return list(self._cache.keys())
