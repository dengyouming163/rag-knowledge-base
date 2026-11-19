"""组装检索链路：作用域校验 + 组件装配。

路由层只负责协议（HTTP/SSE），所有"怎么拼出 retriever"的决策都在这里，
这样测试可以直接调用这些函数，不必经过 HTTP。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.bm25 import BM25Index
from src.bm25_store import BM25Store, IndexNotBuiltError
from src.pipeline import RagPipeline
from src.retrieve import HybridRetriever
from src.store_pg import PgVectorStore

from .errors import ScopeNotFoundError


def ensure_kb(engine: Engine, tenant_id: str, kb_id: str) -> None:
    """知识库必须存在且属于当前租户，否则 404（不区分"不存在"和"不属于你"）。"""
    with engine.connect() as connection:
        exists = connection.execute(
            text(
                "SELECT 1 FROM knowledge_bases WHERE id = :kb_id AND tenant_id = :tenant_id"
            ),
            {"kb_id": kb_id, "tenant_id": tenant_id},
        ).scalar_one_or_none()
    if exists is None:
        raise ScopeNotFoundError(f"knowledge base {kb_id} not found")


def load_bm25(engine: Engine, tenant_id: str, kb_id: str) -> BM25Index:
    """优先读快照；从未构建过则就地重建一次（幂等，1000 条约 0.15s）。"""
    store = BM25Store(engine)
    try:
        return store.load(kb_id)
    except IndexNotBuiltError:
        store.rebuild(tenant_id, kb_id)
        return store.load(kb_id)


def build_retriever(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    embed_client,
    *,
    need_bm25: bool = True,
) -> HybridRetriever:
    vector_store = PgVectorStore(engine, tenant_id=tenant_id, kb_id=kb_id)
    bm25_index = load_bm25(engine, tenant_id, kb_id) if need_bm25 else None
    return HybridRetriever(vector_store, bm25_index, embed_client)


def fetch_parent_texts(
    engine: Engine, tenant_id: str, kb_id: str, parent_ids: list[str]
) -> dict[str, str]:
    """按父块 ID 批量取正文，供命中后补全上下文。"""
    if not parent_ids:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT chunk_id, text FROM chunks "
                "WHERE tenant_id = :tenant_id AND kb_id = :kb_id AND chunk_id = ANY(:ids)"
            ),
            {"tenant_id": tenant_id, "kb_id": kb_id, "ids": parent_ids},
        ).mappings().all()
    return {row["chunk_id"]: row["text"] for row in rows}


def build_pipeline(
    engine: Engine,
    tenant_id: str,
    kb_id: str,
    embed_client,
    chat_client,
    reranker=None,
) -> RagPipeline:
    retriever = build_retriever(engine, tenant_id, kb_id, embed_client)
    return RagPipeline(
        chat_client=chat_client,
        embed_client=embed_client,
        store=engine,
        bm25_index=None,
        retriever=retriever,
        parent_loader=lambda ids: fetch_parent_texts(engine, tenant_id, kb_id, ids),
        reranker=reranker,
    )
