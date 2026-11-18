#!/usr/bin/env python3
"""三路检索对比：vector / bm25 / hybrid 的 top-5 结果与 Recall@5。

用法（无需 API key，--mock 用确定性哈希假向量）：
    py -3.13 scripts/compare_routes.py --mock

真实向量（需要 EMBED_API_KEY）：
    py -3.13 scripts/compare_routes.py --input eval/demo_queries.json --corpus eval/demo_corpus.json

输入格式：[{"query": "...", "gold_chunk_ids": [...]}]
语料格式：[{"chunk_id": "...", "doc_id": "...", "text": "...", ...}]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bm25 import BM25Index, tokenize
from src.chunk import Chunk, estimate_tokens
from src.embed import EmbeddingClient, EmbeddingError
from src.retrieve import HybridRetriever
from src.store import LocalVectorStore

DEFAULT_CORPUS = ROOT / "eval" / "demo_corpus.json"
DEFAULT_INPUT = ROOT / "eval" / "demo_queries.json"


class HashEmbeddingClient:
    """确定性哈希假向量：把分词结果哈希到固定维度，保证同一文本总得到同一向量。"""

    def __init__(self, dim: int = 64) -> None:
        if dim < 1:
            raise ValueError("dim 至少为 1")
        self.dim = dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in tokenize(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        return vector


def load_chunks(path: Path) -> list[Chunk]:
    data = json.loads(path.read_text(encoding="utf-8"))
    chunks: list[Chunk] = []
    for item in data:
        text = item["text"]
        chunks.append(
            Chunk(
                chunk_id=item["chunk_id"],
                doc_id=item.get("doc_id") or item["chunk_id"].split("#")[0],
                text=text,
                title=item.get("title", ""),
                section_path=list(item.get("section_path", [])),
                source_url=item.get("source_url", ""),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                audience=item.get("audience", "public"),
                token_count=item.get("token_count") or estimate_tokens(text),
            )
        )
    return chunks


def load_queries(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("查询文件必须是数组：[{\"query\": ..., \"gold_chunk_ids\": [...]}]")
    for item in data:
        if "query" not in item or "gold_chunk_ids" not in item:
            raise ValueError("每条查询都必须包含 query 与 gold_chunk_ids")
    return data


def recall_at_k(chunk_ids: list[str], gold: list[str]) -> float:
    """被 top-k 覆盖的 gold 比例；gold 为空按 0 处理。"""
    if not gold:
        return 0.0
    return len(set(chunk_ids) & set(gold)) / len(gold)


def build_retriever(chunks: list[Chunk], embed) -> HybridRetriever:
    store = LocalVectorStore(tempfile.mkdtemp(prefix="rag-compare-"))
    store.upsert(chunks, embed.embed_texts([chunk.text for chunk in chunks]))
    return HybridRetriever(store, BM25Index(chunks), embed)


def format_route(label: str, rows: list[tuple[str, float]], gold: list[str]) -> str:
    if not rows:
        return f"  {label:<6}: （无结果）"
    cells = [f"{chunk_id}[{'OK' if chunk_id in gold else '--'}]" for chunk_id, _ in rows]
    return f"  {label:<6}: " + "  ".join(cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vector / bm25 / hybrid 三路检索对比")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="查询 JSON 文件")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="语料 JSON 文件")
    parser.add_argument("--mock", action="store_true", help="用确定性哈希假向量，不联网")
    parser.add_argument("--top-k", type=int, default=5, help="每个 query 展示的结果数")
    parser.add_argument("--recall-k", type=int, default=40, help="每路召回的候选数")
    parser.add_argument("--dim", type=int, default=64, help="mock 向量维度")
    args = parser.parse_args(argv)

    if args.mock:
        embed = HashEmbeddingClient(dim=args.dim)
        print(f"模式：mock（{args.dim} 维哈希假向量，仅验证流程，不代表真实检索效果）")
    else:
        try:
            embed = EmbeddingClient()
        except EmbeddingError as exc:
            print(f"初始化 Embedding 失败：{exc}", file=sys.stderr)
            print("无 API key 时请改用 --mock 运行。", file=sys.stderr)
            return 1

    chunks = load_chunks(args.corpus)
    queries = load_queries(args.input)
    if not chunks:
        print("语料为空，无法对比。", file=sys.stderr)
        return 1

    retriever = build_retriever(chunks, embed)
    totals = {mode: 0.0 for mode in ("vector", "bm25", "hybrid")}

    print(f"语料 {len(chunks)} 块，查询 {len(queries)} 条，top_k={args.top_k}\n")
    for index, item in enumerate(queries, start=1):
        query = item["query"]
        gold = list(item["gold_chunk_ids"])
        report = retriever.compare_routes(
            query, top_k=args.top_k, recall_k=args.recall_k
        )

        print(f"[{index}/{len(queries)}] 查询：{query}")
        print(f"  {'gold':<6}: " + "  ".join(gold))
        for mode in ("vector", "bm25", "hybrid"):
            rows = report[mode]
            print(format_route(mode, rows, gold))
            totals[mode] += recall_at_k([chunk_id for chunk_id, _ in rows], gold)
        print()

    count = len(queries) or 1
    print("=" * 60)
    print(f"Recall@{args.top_k} 汇总（查询数 {len(queries)}）")
    for mode in ("vector", "bm25", "hybrid"):
        print(f"  {mode:<6}: {totals[mode] / count:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
