"""pgvector 版向量库：与 LocalVectorStore 同接口，但强制租户/知识库过滤。

设计要点：
1. 过滤键 tenant_id 与 kb_id 是**必填**的。缺失时抛 MissingTenantFilterError，
   而不是"查全表"——这是多租户系统最贵的一类事故（跨租户泄露）。
2. 作用域可以来自构造参数，也可以来自每次调用的 filters，后者优先。
3. 相似度统一用 `1 - 余弦距离`，与 LocalVectorStore 的点积结果同分布，契约测试才能比对。
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .acl import acl_condition
from .chunk import Chunk
from .store import SearchHit


class MissingTenantFilterError(ValueError):
    """缺少租户或知识库过滤条件。属于代码缺陷，映射到 HTTP 500 而不是 400。"""


class PgVectorStore:
    """基于 Postgres + pgvector 的向量库实现。

    用法（单库作用域）：
        store = PgVectorStore(engine, tenant_id="demo", kb_id="kb_after_sale")
        store.upsert(chunks, vectors)
        store.search(query_vector, top_k=5, filters={"audience": "public"})

    用法（多库作用域）：
        store = PgVectorStore(engine, tenant_id="demo")
        store.search(qv, top_k=5, filters={"kb_id": ["kb_after_sale", "kb_logistics"]})
    """

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: str | None = None,
        kb_id: str | None = None,
        embed_model_version: str | None = None,
    ) -> None:
        self._engine = engine
        self._tenant_id = tenant_id
        self._kb_id = kb_id
        self._embed_model_version = embed_model_version or os.environ.get(
            "EMBED_MODEL", "unknown"
        )

    # ------------------------------------------------------------------ 写入

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """按 (tenant_id, kb_id, content_hash) 幂等写入，返回处理行数。

        注意：本方法不负责创建 documents 行，文档元数据由入库脚本先写入，
        保证一篇文档的所有块挂在同一个文档记录下。
        """
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量必须一致")

        tenant_id, kb_ids = self._resolve_scope(None)
        if len(kb_ids) != 1:
            raise ValueError("写入时必须指定唯一 kb_id")
        kb_id = kb_ids[0]

        sql = text(
            """
            INSERT INTO chunks (
                chunk_id, tenant_id, kb_id, doc_id, content_hash, text,
                token_count, embedding, embed_model_version, meta,
                parent_id, is_parent
            ) VALUES (
                :chunk_id, :tenant_id, :kb_id, :doc_id, :content_hash, :text,
                :token_count, CAST(:embedding AS vector), :model_version,
                CAST(:meta AS jsonb), :parent_id, :is_parent
            )
            ON CONFLICT (tenant_id, kb_id, content_hash) DO UPDATE SET
                text = EXCLUDED.text,
                token_count = EXCLUDED.token_count,
                embedding = EXCLUDED.embedding,
                embed_model_version = EXCLUDED.embed_model_version,
                meta = EXCLUDED.meta,
                parent_id = EXCLUDED.parent_id,
                is_parent = EXCLUDED.is_parent,
                updated_at = now()
            """
        )

        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "tenant_id": tenant_id,
                "kb_id": kb_id,
                "doc_id": chunk.doc_id,
                "content_hash": chunk.content_hash,
                "text": chunk.text,
                "token_count": chunk.token_count,
                "embedding": None if vector is None else _vector_literal(vector),
                "model_version": self._embed_model_version,
                "meta": _meta_json(chunk),
                "parent_id": chunk.parent_id,
                "is_parent": bool(chunk.is_parent),
            }
            for chunk, vector in zip(chunks, vectors)
        ]

        with self._engine.begin() as connection:
            connection.execute(sql, rows)
        return len(rows)

    def delete_by_doc(self, doc_id: str) -> int:
        tenant_id, kb_ids = self._resolve_scope(None)
        condition = "tenant_id = :tenant_id AND doc_id = :doc_id AND kb_id = ANY(CAST(:kb_ids AS text[]))"
        with self._engine.begin() as connection:
            result = connection.execute(
                text(f"DELETE FROM chunks WHERE {condition}"),
                {"tenant_id": tenant_id, "doc_id": doc_id, "kb_ids": kb_ids},
            )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------------ 查询

    def search(
        self,
        query_vector: list[float],
        top_k: int = 10,
        filters: dict | None = None,
    ) -> list[SearchHit]:
        if top_k < 1:
            raise ValueError("top_k 至少为 1")

        tenant_id, kb_ids = self._resolve_scope(filters)
        audience = (filters or {}).get("audience")
        user_tags = list((filters or {}).get("user_tags") or [])
        is_admin = bool((filters or {}).get("is_admin"))

        conditions = [
            "tenant_id = :tenant_id",
            "kb_id = ANY(CAST(:kb_ids AS text[]))",
            "embedding IS NOT NULL",
            # 父块只用于补上下文，不参与召回
            "is_parent = false",
        ]
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "kb_ids": kb_ids,
            "query_vector": _vector_literal(query_vector),
            "top_k": top_k,
        }

        # 文档级 ACL：在打分前过滤，避免无权结果占满 top_k
        acl_sql, acl_params = acl_condition(user_tags, is_admin)
        conditions.append(acl_sql)
        params.update(acl_params)
        if audience is not None:
            conditions.append("meta->>'audience' = :audience")
            params["audience"] = audience

        sql = text(
            f"""
            SELECT chunk_id, doc_id, text, meta, parent_id,
                   1 - (embedding <=> CAST(:query_vector AS vector)) AS score
            FROM chunks
            WHERE {' AND '.join(conditions)}
            ORDER BY embedding <=> CAST(:query_vector AS vector), chunk_id
            LIMIT :top_k
            """
        )

        with self._engine.connect() as connection:
            rows = connection.execute(sql, params).mappings().all()

        return [
            SearchHit(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                text=row["text"],
                score=float(row["score"]),
                metadata={
                    **dict(row["meta"] or {}),
                    **({"parent_id": row["parent_id"]} if row["parent_id"] else {}),
                },
            )
            for row in rows
        ]

    def count(self) -> int:
        tenant_id, kb_ids = self._resolve_scope(None)
        with self._engine.connect() as connection:
            return int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM chunks "
                        "WHERE tenant_id = :tenant_id "
                        "AND kb_id = ANY(CAST(:kb_ids AS text[]))"
                    ),
                    {"tenant_id": tenant_id, "kb_ids": kb_ids},
                ).scalar_one()
            )

    # ------------------------------------------------------------------ 内部

    def _resolve_scope(self, filters: dict | None) -> tuple[str, list[str]]:
        """合并构造参数与调用参数，任何一项缺失都立即失败。"""
        merged = dict(filters or {})
        tenant_id = merged.pop("tenant_id", None) or self._tenant_id
        kb_id = merged.pop("kb_id", None) or self._kb_id

        if not tenant_id:
            raise MissingTenantFilterError(
                "缺少 tenant_id：检索与写入都必须带租户过滤，禁止跨租户扫描"
            )
        if not kb_id:
            raise MissingTenantFilterError(
                "缺少 kb_id：检索与写入都必须限定知识库，禁止跨库扫描"
            )

        kb_ids = [kb_id] if isinstance(kb_id, str) else list(kb_id)
        if not kb_ids:
            raise MissingTenantFilterError("kb_id 列表为空，拒绝执行全库扫描")
        return str(tenant_id), [str(item) for item in kb_ids]


def _vector_literal(vector: Sequence[float]) -> str:
    """转成 pgvector 能接受的文本字面量，如 `[0.1,0.2]`。"""
    if not vector:
        raise ValueError("向量不能为空")
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _meta_json(chunk: Chunk) -> str:
    import json

    return json.dumps(
        {
            "section_path": list(chunk.section_path),
            "title": chunk.title,
            "audience": chunk.audience,
            "source_url": chunk.source_url,
            "acl_tags": list(chunk.acl_tags),
        },
        ensure_ascii=False,
    )
