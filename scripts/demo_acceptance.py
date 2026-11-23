"""Gate C 演示脚本：一条命令验证「检索 / 拒答边界 / 隔离 / ACL / 版本」。

不依赖 HTTP、不依赖真实 Key：使用与入库时相同的确定性假向量，
因此结果可复现。演示结束会自动还原它修改过的数据（标签、版本指针）。

用法：
    py -3.13 scripts/demo_acceptance.py
退出码：0 = 全部通过；1 = 有失败项。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT, ROOT / "scripts"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from compare_routes import HashEmbeddingClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from src.acl import set_chunk_acl  # noqa: E402
from src.bm25 import BM25Index  # noqa: E402
from src.chunk import Chunk, estimate_tokens  # noqa: E402
from src.db.base import app_database_url, create_db_engine  # noqa: E402
from src.db.session import tenant_scope  # noqa: E402
from src.retrieve import HybridRetriever  # noqa: E402
from src.store_pg import PgVectorStore  # noqa: E402
from src.versioning import publish, rollback  # noqa: E402

TENANT = "demo"
KB = "kb_after_sale"
CHUNK_PREFIX = "kb-after-sale"  # 历史数据的 chunk_id 用连字符，保持不变
OTHER_TENANT = "demo_other"
DIM = 1024

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))


def load_chunks(engine) -> list[Chunk]:
    """从库里读回 chunk，供 BM25 建索引（与线上加载路径一致）。"""
    with tenant_scope(engine, TENANT) as connection:
        rows = connection.execute(
            text(
                "SELECT chunk_id, doc_id, text, content_hash, token_count, meta FROM chunks "
                "WHERE tenant_id = :t AND kb_id = :kb AND is_parent = false ORDER BY chunk_id"
            ),
            {"t": TENANT, "kb": KB},
        ).mappings().all()

    chunks = []
    for row in rows:
        meta = row["meta"] or {}
        chunks.append(
            Chunk(
                chunk_id=row["chunk_id"],
                doc_id=row["doc_id"],
                text=row["text"],
                title=meta.get("title", ""),
                section_path=list(meta.get("section_path") or []),
                source_url=meta.get("source_url", ""),
                content_hash=row["content_hash"],
                audience=meta.get("audience", "public"),
                token_count=row["token_count"] or estimate_tokens(row["text"]),
            )
        )
    return chunks


def main() -> int:
    engine = create_db_engine()
    embed = HashEmbeddingClient(dim=DIM)
    store = PgVectorStore(engine, tenant_id=TENANT, kb_id=KB, embed_model_version="mock")

    chunks = load_chunks(engine)
    if not chunks:
        print(f"[SKIP] 库中没有 {KB} 的数据，请先运行 scripts/migrate_json_to_pg.py")
        return 1

    retriever = HybridRetriever(store, BM25Index(chunks), embed)
    query = "退款 多久 到账"

    # 1. 混合检索能命中退款政策
    hits = retriever.retrieve(query, top_k=5, filters={"user_tags": []}, mode="hybrid")
    ids = [hit.chunk_id for hit in hits]
    check("混合检索命中", f"{CHUNK_PREFIX}#1" in ids, f"top={ids[:3]}")

    # 2. 跨租户检索为空
    other = PgVectorStore(engine, tenant_id=OTHER_TENANT, kb_id=KB)
    cross = other.search(embed.embed_texts([query])[0], top_k=5, filters={"is_admin": True})
    check("跨租户隔离", cross == [], f"命中 {len(cross)} 条")

    # 3. RLS：未声明作用域查不到任何行
    with engine.connect() as connection:
        raw = connection.execute(text("SELECT count(*) FROM chunks")).scalar_one()
    check("RLS 兜底", raw == 0, f"裸连接 count={raw}")

    # 4. ACL：临时给 #2 打标签，匿名看不到、法务能看到
    target = f"{CHUNK_PREFIX}#2"
    try:
        set_chunk_acl(engine, TENANT, [target], ["dept:legal"])
        visitor = retriever.retrieve(query, top_k=10, filters={"user_tags": []}, mode="hybrid")
        legal = retriever.retrieve(
            query, top_k=10, filters={"user_tags": ["dept:legal"]}, mode="hybrid"
        )
        visitor_ids = [hit.chunk_id for hit in visitor]
        legal_ids = [hit.chunk_id for hit in legal]
        check("ACL 拒绝越权", target not in visitor_ids, f"匿名 top={visitor_ids[:3]}")
        check("ACL 放行本部门", target in legal_ids, f"法务 top={legal_ids[:3]}")
    finally:
        set_chunk_acl(engine, TENANT, [target], [])

    # 5. 版本：发布一个快照，回滚后可见集等于该快照
    try:
        released = publish(engine, TENANT, KB, note="demo")
        restored = rollback(engine, TENANT, KB, released["version_id"])
        pinned = store.search(
            embed.embed_texts([query])[0],
            top_k=10,
            filters={"version_chunk_ids": restored["chunk_ids"]},
        )
        check(
            "版本发布与回滚",
            {hit.chunk_id for hit in pinned} == set(restored["chunk_ids"]),
            f"v{restored['version']} / {restored['chunk_count']} 条",
        )
    finally:
        with tenant_scope(engine, TENANT, write=True) as connection:
            connection.execute(
                text("DELETE FROM kb_versions WHERE tenant_id = :t AND kb_id = :kb"),
                {"t": TENANT, "kb": KB},
            )
            connection.execute(
                text(
                    "UPDATE knowledge_bases SET active_version_id = NULL "
                    "WHERE tenant_id = :t AND id = :kb"
                ),
                {"t": TENANT, "kb": KB},
            )

    engine.dispose()

    width = max(len(name) for name, _, _ in results)
    print(f"\n{'场景'.ljust(width)}  结果   说明")
    print("-" * (width + 40))
    for name, ok, detail in results:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}   {detail}")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print(f"数据库：{app_database_url().split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
