#!/usr/bin/env python3
"""按 embedding 版本重嵌 chunks：幂等、可续跑、支持 dry-run。

    py -3.13 scripts/reembed_chunks.py --tenant demo --dry-run
    py -3.13 scripts/reembed_chunks.py --tenant demo --kb kb_after_sale --only mock-hash

`--only` 是源版本筛选：`mock-hash` → `IN ('mock-hash','unknown','')`；
`bulk-random` → 该值；`all` → 只选与目标版本不一致的行。
目标版本取 `embed.model`，哈希客户端没有 model，回退 `mock-hash`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from src.db.base import create_db_engine  # noqa: E402
from src.db.session import tenant_scope  # noqa: E402

BATCH = 16
ONLY_FILTERS = {"mock-hash": ("mock-hash", "unknown", ""), "bulk-random": ("bulk-random",)}


def target_version(embed) -> str:
    return str(getattr(embed, "model", "") or "mock-hash")


def _vector(values) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def _select(connection, *, kb_id, only, target, limit):
    where = ["(embedding IS NULL OR embed_model_version <> :target)"]
    params: dict = {"target": target, "limit": limit}
    if kb_id:
        where.append("kb_id = :kb")
        params["kb"] = kb_id
    if only != "all":
        marks = ", ".join(f":v{i}" for i in range(len(ONLY_FILTERS[only])))
        where.append(f"embed_model_version IN ({marks})")
        params.update({f"v{i}": v for i, v in enumerate(ONLY_FILTERS[only])})
    sql = (
        "SELECT chunk_id, text FROM chunks WHERE "
        + " AND ".join(where)
        + " ORDER BY chunk_id LIMIT :limit"
    )
    return connection.execute(text(sql), params).mappings().all()


def reembed(
    engine, embed, *, tenant_id, kb_id=None, only="all", limit=None, dry_run=False
) -> dict:
    if only not in (*ONLY_FILTERS, "all"):
        raise ValueError(f"unknown --only: {only}")
    target = target_version(embed)
    if dry_run:
        with tenant_scope(engine, tenant_id) as connection:
            rows = _select(
                connection, kb_id=kb_id, only=only, target=target, limit=limit or 1_000_000
            )
        return {"target_version": target, "scanned": len(rows), "updated": 0, "dry_run": True}
    scanned = 0
    with tenant_scope(engine, tenant_id, write=True) as connection:
        while limit is None or scanned < limit:
            rows = _select(
                connection,
                kb_id=kb_id,
                only=only,
                target=target,
                limit=min(BATCH, limit - scanned) if limit else BATCH,
            )
            if not rows:
                break
            for row, vector in zip(
                rows, embed.embed_texts([r["text"] for r in rows]), strict=False
            ):
                connection.execute(
                    text(
                        "UPDATE chunks SET embedding = CAST(:e AS vector), "
                        "embed_model_version = :target, updated_at = now() "
                        "WHERE chunk_id = :id AND tenant_id = :t"
                    ),
                    {
                        "e": _vector(vector),
                        "target": target,
                        "id": row["chunk_id"],
                        "t": tenant_id,
                    },
                )
            scanned += len(rows)
    return {"target_version": target, "scanned": scanned, "updated": scanned, "dry_run": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按 embedding 版本重嵌 chunks")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--kb")
    parser.add_argument("--only", choices=("mock-hash", "bulk-random", "all"), default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from app.deps import get_embedder

    engine = create_db_engine()
    try:
        print(
            reembed(
                engine,
                get_embedder(),
                tenant_id=args.tenant,
                kb_id=args.kb,
                only=args.only,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
