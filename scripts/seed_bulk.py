#!/usr/bin/env python3
"""批量灌入压测/演示数据（默认 10000 条 chunk）。

设计约束：
1. 不污染评测语料：数据落在独立的 `kb_bulk_*` 知识库，评测用的 `kb_*` 保持原样。
2. 向量由 Postgres 侧生成（1024 维随机后 L2 归一化），不调外部 API，几秒完成。
3. 幂等：chunk_id 形如 `bulk-0000123`，重复执行走 ON CONFLICT DO NOTHING。

注意：这批向量是**随机方向**，没有语义。它的用途是看数据量、看分页、看执行计划，
不能用来证明"检索准不准"——后者仍然由 eval/cases.jsonl 的 26 条语料负责。

用法：
    py -3.13 scripts/seed_bulk.py --chunks 10000
    py -3.13 scripts/seed_bulk.py --chunks 10000 --reset
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from src.db.base import create_db_engine

TENANT_ID = "demo"
KB_PREFIX = "kb_bulk_"


def seed(engine, total: int, kbs: int, docs_per_kb: int, reset: bool) -> None:
    chunks_per_kb = max(1, total // kbs)
    chunks_per_doc = max(1, chunks_per_kb // docs_per_kb)

    with engine.begin() as connection:
        # chunks 开了 RLS：整个事务声明租户作用域
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_ID}
        )
        if reset:
            connection.execute(
                text("DELETE FROM chunks WHERE tenant_id = :t AND chunk_id LIKE 'bulk-%'"),
                {"t": TENANT_ID},
            )
            connection.execute(
                text("DELETE FROM documents WHERE tenant_id = :t AND id LIKE 'bulk-doc-%'"),
                {"t": TENANT_ID},
            )
            connection.execute(
                text("DELETE FROM knowledge_bases WHERE tenant_id = :t AND id LIKE 'kb\\_bulk\\_%'"),
                {"t": TENANT_ID},
            )

        connection.execute(
            text(
                "INSERT INTO tenants (id, name) VALUES (:id, '演示租户') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": TENANT_ID},
        )

        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "SELECT :prefix || lpad(i::text, 2, '0'), :t, '压测知识库 ' || i, '', 'active' "
                "FROM generate_series(1, :kbs) i "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"prefix": KB_PREFIX, "t": TENANT_ID, "kbs": kbs},
        )

        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "SELECT 'bulk-doc-' || lpad(k::text, 2, '0') || '-' || lpad(d::text, 2, '0'), "
                "       :t, :prefix || lpad(k::text, 2, '0'), "
                "       '压测文档 ' || k || '-' || d, '', md5(k || '-' || d), 'ready', "
                "       jsonb_build_object('bulk', true) "
                "FROM generate_series(1, :kbs) k, generate_series(1, :docs) d "
                "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING"
            ),
            {"prefix": KB_PREFIX, "t": TENANT_ID, "kbs": kbs, "docs": docs_per_kb},
        )

        connection.execute(
            text(
                "INSERT INTO chunks "
                "(chunk_id, tenant_id, kb_id, doc_id, content_hash, text, token_count, "
                " embedding, embed_model_version, meta) "
                "SELECT 'bulk-' || lpad(s.i::text, 7, '0'), "
                "       :t, "
                "       :prefix || lpad((((s.i - 1) / :per_kb) + 1)::text, 2, '0'), "
                "       'bulk-doc-' || lpad((((s.i - 1) / :per_kb) + 1)::text, 2, '0') || '-' || "
                "         lpad(((((s.i - 1) % :per_kb) / :per_doc) + 1)::text, 2, '0'), "
                "       md5('bulk-' || s.i::text), "
                "       '第 ' || s.i || ' 条：设备 X200-Pro 的保修期为 ' "
                "         || (12 + s.i % 12) || ' 个月，维修网点覆盖全国主要城市。', "
                "       24, "
                "       l2_normalize((SELECT array_agg(random()::real) "
                "                     FROM generate_series(1, 1024))::vector), "
                "       'bulk-random', "
                "       jsonb_build_object('audience', 'public', 'bulk', true) "
                "FROM generate_series(1, :total) s(i) "
                "ON CONFLICT (chunk_id) DO NOTHING"
            ),
            {
                "prefix": KB_PREFIX,
                "t": TENANT_ID,
                "total": total,
                "per_kb": chunks_per_kb,
                "per_doc": chunks_per_doc,
            },
        )

        # 统计信息过旧会让 planner 选错计划，插入后立刻 ANALYZE
        connection.execute(text("ANALYZE chunks"))


def report(engine) -> None:
    with engine.connect() as connection:
        total = connection.execute(
            text("SELECT count(*) FROM chunks WHERE tenant_id = :t"), {"t": TENANT_ID}
        ).scalar_one()
        bulk = connection.execute(
            text("SELECT count(*) FROM chunks WHERE tenant_id = :t AND chunk_id LIKE 'bulk-%'"),
            {"t": TENANT_ID},
        ).scalar_one()
        size = connection.execute(
            text("SELECT pg_size_pretty(pg_total_relation_size('chunks'))")
        ).scalar_one()
        index_size = connection.execute(
            text(
                "SELECT pg_size_pretty(pg_relation_size('ix_chunks_embedding_hnsw'))"
            )
        ).scalar_one()
    print(f"chunks 总数：{total}（其中压测数据 {bulk}）")
    print(f"表占用：{size}，HNSW 索引：{index_size}")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量灌入压测数据")
    parser.add_argument("--chunks", type=int, default=10000)
    parser.add_argument("--kbs", type=int, default=10, help="拆成几个知识库")
    parser.add_argument("--docs-per-kb", type=int, default=10)
    parser.add_argument("--reset", action="store_true", help="先删除已有压测数据")
    args = parser.parse_args()

    engine = create_db_engine()
    started = time.perf_counter()
    try:
        seed(engine, args.chunks, args.kbs, args.docs_per_kb, args.reset)
        elapsed = time.perf_counter() - started
        print(f"灌入完成，用时 {elapsed:.1f}s")
        report(engine)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
