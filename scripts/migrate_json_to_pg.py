#!/usr/bin/env python3
"""把旧 JSON 语料幂等地导入 Postgres（tenants → knowledge_bases → documents → chunks）。

用法：
    # 无 Key、确定性（默认，CI/本地都用这个）
    py -3.13 scripts/migrate_json_to_pg.py --mock

    # 真实 embedding（需要 EMBED_API_KEY），向量才能真正用于语义检索
    py -3.13 scripts/migrate_json_to_pg.py --real

    # 指定其他来源（兼容旧版 vectors.json 导出）
    py -3.13 scripts/migrate_json_to_pg.py --source data/store.json --mock

幂等的保证来自三处：
1. tenants / knowledge_bases 用 ON CONFLICT DO NOTHING；
2. documents / chunks 用 ON CONFLICT (tenant_id, kb_id, content_hash)；
3. 重复执行时脚本会打印"导入前后总数不变"作为证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import text

from compare_routes import HashEmbeddingClient
from src.chunk import Chunk, estimate_tokens
from src.db.base import create_db_engine
from src.embed import EmbeddingClient, EmbeddingError
from src.store_pg import PgVectorStore

DEFAULT_SOURCE = ROOT / "eval" / "demo_corpus.json"
EMBED_DIM = 1024


def load_chunks(source: Path) -> list[Chunk]:
    """读取语料，兼容两种格式：评测语料数组、旧版 store.json 的 records。"""
    payload = json.loads(source.read_text(encoding="utf-8"))

    if isinstance(payload, dict):
        records = payload.get("records", [])
        items = [
            {
                "chunk_id": record["chunk_id"],
                "doc_id": record["doc_id"],
                "text": record["text"],
                "title": record["metadata"].get("title", ""),
                "section_path": record["metadata"].get("section_path", []),
                "source_url": record["metadata"].get("source_url", ""),
                "audience": record["metadata"].get("audience", "public"),
            }
            for record in records
        ]
    else:
        items = payload

    chunks: list[Chunk] = []
    for item in items:
        text_value = item["text"]
        chunks.append(
            Chunk(
                chunk_id=item["chunk_id"],
                doc_id=item["doc_id"],
                text=text_value,
                title=item.get("title", ""),
                section_path=item.get("section_path", []),
                source_url=item.get("source_url", ""),
                content_hash=item.get("content_hash")
                or hashlib.sha256(text_value.encode("utf-8")).hexdigest()[:16],
                audience=item.get("audience", "public"),
                token_count=estimate_tokens(text_value),
            )
        )
    return chunks


def kb_id_for_doc(doc_id: str) -> str:
    """文档 → 知识库的映射：`kb-after-sale` → `kb_after_sale`。

    保持 doc_id 与 chunk_id 原样不变（评测基线依赖它们），只新增下划线形式的 kb_id。
    """
    return doc_id.replace("-", "_")


def doc_content_hash(chunks: list[Chunk]) -> str:
    """整篇文档的指纹：用所有块文本拼接后取前 16 位。"""
    joined = "\n".join(chunk.text for chunk in chunks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def upsert_structure(engine, tenant_id: str, chunks: list[Chunk]) -> dict[str, int]:
    """写入租户、知识库、文档三层结构，返回各表处理数量。"""
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.doc_id, []).append(chunk)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name) VALUES (:id, :name) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": tenant_id, "name": "演示租户"},
        )

        for doc_id, doc_chunks in by_doc.items():
            kb_id = kb_id_for_doc(doc_id)
            title = doc_chunks[0].title or doc_id
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                    "VALUES (:id, :tenant_id, :name, '', 'active') "
                    "ON CONFLICT (id) DO NOTHING"
                ),
                {"id": kb_id, "tenant_id": tenant_id, "name": title},
            )
            connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:id, :tenant_id, :kb_id, :title, :source_url, :content_hash, "
                    "'ready', '{}'::jsonb) "
                    "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING"
                ),
                {
                    "id": doc_id,
                    "tenant_id": tenant_id,
                    "kb_id": kb_id,
                    "title": title,
                    "source_url": doc_chunks[0].source_url,
                    "content_hash": doc_content_hash(doc_chunks),
                },
            )

    return {"documents": len(by_doc), "knowledge_bases": len({kb_id_for_doc(d) for d in by_doc})}


def build_embedder(mock: bool):
    if mock:
        # 与生产表维度保持一致，否则 Postgres 会拒绝写入
        return HashEmbeddingClient(dim=EMBED_DIM)
    try:
        return EmbeddingClient()
    except EmbeddingError as exc:
        print(f"初始化 Embedding 失败：{exc}", file=sys.stderr)
        print("请设置 EMBED_API_KEY，或改用 --mock。", file=sys.stderr)
        raise SystemExit(1)


def migrate(engine, tenant_id: str, chunks: list[Chunk], embedder) -> dict[str, int]:
    by_kb: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_kb.setdefault(kb_id_for_doc(chunk.doc_id), []).append(chunk)

    written = 0
    for kb_id, kb_chunks in by_kb.items():
        vectors = embedder.embed_texts([chunk.text for chunk in kb_chunks])
        for vector in vectors:
            if len(vector) != EMBED_DIM:
                raise SystemExit(
                    f"向量维度不一致：期望 {EMBED_DIM}，实际 {len(vector)}（检查 Embedding 模型）"
                )
        store = PgVectorStore(
            engine,
            tenant_id=tenant_id,
            kb_id=kb_id,
            embed_model_version="mock-hash" if hasattr(embedder, "dim") else "remote",
        )
        written += store.upsert(kb_chunks, vectors)
    return {"chunks_written": written}


def count_chunks(engine, tenant_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM chunks WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )


def verify_samples(engine, tenant_id: str, chunks: list[Chunk]) -> int:
    """抽查 5 条：文本与 content_hash 必须与来源一致。返回不一致条数。"""
    sample = chunks[:: max(1, len(chunks) // 5)][:5]
    mismatches = 0
    with engine.connect() as connection:
        for expected in sample:
            row = connection.execute(
                text(
                    "SELECT text, content_hash FROM chunks "
                    "WHERE tenant_id = :tenant_id AND chunk_id = :chunk_id"
                ),
                {"tenant_id": tenant_id, "chunk_id": expected.chunk_id},
            ).mappings().first()
            if row is None or row["text"] != expected.text or row["content_hash"] != expected.content_hash:
                mismatches += 1
                print(f"  ✗ {expected.chunk_id} 与来源不一致")
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧 JSON 语料幂等导入 Postgres")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mock", action="store_true", help="使用 1024 维哈希向量，不调外部 API")
    group.add_argument("--real", action="store_true", help="调用 EMBED_API_KEY 对应的真实模型")
    parser.add_argument("--tenant", default="demo")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"找不到语料文件：{args.source}", file=sys.stderr)
        return 2

    chunks = load_chunks(args.source)
    engine = create_db_engine()
    try:
        before = count_chunks(engine, args.tenant)
        structure = upsert_structure(engine, args.tenant, chunks)
        embedder = build_embedder(args.mock)
        result = migrate(engine, args.tenant, chunks, embedder)
        after = count_chunks(engine, args.tenant)
        mismatches = verify_samples(engine, args.tenant, chunks)
    finally:
        engine.dispose()

    print(f"来源：{args.source}")
    print(f"租户：{args.tenant}")
    print(f"知识库：{structure['knowledge_bases']}，文档：{structure['documents']}")
    print(f"本次处理 chunks：{result['chunks_written']}，来源共 {len(chunks)} 条")
    print(f"导入前总数：{before}，导入后总数：{after}")
    print(f"抽查不一致：{mismatches} 条")

    if mismatches:
        return 1
    if after != len(chunks):
        # 重复执行时 before 已经等于 len(chunks)，after 也应保持一致
        print("警告：库里 chunk 数与来源不一致（可能存在脏数据）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
