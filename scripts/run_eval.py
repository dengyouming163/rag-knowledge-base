#!/usr/bin/env python3
"""在固定评测集上跑一遍，输出可复现的指标快照。

用法：
    # 无 Key、确定性（CI 用这个）
    py -3.13 scripts/run_eval.py --mock --out eval/baseline.json

    # 真实 embedding（需要 EMBED_API_KEY），用于记录质量基线
    py -3.13 scripts/run_eval.py --real --out eval/baseline_real.json

    # 接生成链路：跑完整的 RAG（检索 → 生成 → 引用校验）
    py -3.13 scripts/run_eval.py --real --generate --out eval/baseline_real_gen.json

输出包含：跑的是哪份集、什么模式、top-k、各指标、逐条结果。
不加 --generate 时，拒答率 / 覆盖率 / 引用准确率记为 null（检索指标不依赖生成链路）；
加 --generate 后这三项才有值，代价是每条用例要调两次 LLM（改写 + 回答）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from compare_routes import HashEmbeddingClient
from src.bm25 import BM25Index
from src.chunk import Chunk, estimate_tokens
from src.citation import extract_ids
from src.embed import EmbeddingClient, EmbeddingError
from src.llm import ChatClient, LLMError
from src.metrics import mrr, recall_at_k
from src.pipeline import RagPipeline
from src.retrieve import MODES, HybridRetriever
from src.store import LocalVectorStore

EVAL_DIR = ROOT / "eval"
DEFAULT_CORPUS = EVAL_DIR / "demo_corpus.json"
DEFAULT_SUITE = EVAL_DIR / "cases.jsonl"
DEFAULT_OUT = EVAL_DIR / "baseline.json"


def load_corpus(path: Path) -> list[Chunk]:
    """评测语料已经是切好的 chunk，直接构造成 Chunk 对象，不再重新切分。

    这么做是为了保证 expected_chunk_ids 稳定：如果每次跑评测都重新切分，
    chunk_id 会变，标注就全失效了。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    chunks: list[Chunk] = []
    for item in raw:
        text = item["text"]
        chunks.append(
            Chunk(
                chunk_id=item["chunk_id"],
                doc_id=item["doc_id"],
                text=text,
                title=item.get("title", ""),
                section_path=item.get("section_path", []),
                source_url=item.get("source_url", ""),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                audience=item.get("audience", "public"),
                token_count=estimate_tokens(text),
            )
        )
    return chunks


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_retriever(mock: bool, chunks: list[Chunk]) -> tuple[HybridRetriever, object]:
    if mock:
        embed = HashEmbeddingClient(dim=64)
    else:
        try:
            embed = EmbeddingClient()
        except EmbeddingError as exc:
            print(f"初始化 Embedding 失败：{exc}", file=sys.stderr)
            print("请设置 EMBED_API_KEY，或改用 --mock。", file=sys.stderr)
            raise SystemExit(1)

    vectors = embed.embed_texts([chunk.text for chunk in chunks])
    store = LocalVectorStore(tempfile.mkdtemp(prefix="rag-eval-"))
    store.upsert(chunks, vectors)
    bm25 = BM25Index(chunks)
    return HybridRetriever(store, bm25, embed), embed


REFUSAL_MARKERS = ("无法确定", "无法回答", "没有找到", "不足以回答", "无法根据")


def _is_refusal(answer: str, chunks: list[str]) -> bool:
    """判断这次回答算不算"拒答"。

    只看 chunks 是否为空是不够的：检索总能返回几条命中，
    模型完全可能自己写出"根据现有资料无法确定"。那也是一种正确拒答。
    """
    if not chunks:
        return True
    return any(marker in answer for marker in REFUSAL_MARKERS)


def _generate_case(pipeline: RagPipeline, case_id: str, query: str, top_k: int) -> dict:
    """跑一次完整生成链路，返回逐条记录。

    单条 LLM 失败不能中断整轮评测（50 条里挂一条就白跑）；
    失败记为 error，该条不计入生成类指标的分母。
    """
    try:
        out = pipeline.answer(query, top_k=top_k)
    except LLMError as exc:
        print(f"[WARN] {case_id} 生成失败：{exc}", file=sys.stderr)
        return {
            "answer": None,
            "cited": [],
            "refused": None,
            "citation_accuracy": None,
            "citation_ok": None,
            "error": str(exc),
        }

    answer = out["answer"]
    return {
        # 截断只为控制报告体积；校验用的是完整回答
        "answer": answer[:300],
        "cited": extract_ids(answer),
        "refused": _is_refusal(answer, out["chunks"]),
        "citation_accuracy": out["citation_accuracy"],
        "citation_ok": out["citation_ok"],
        "error": None,
    }


def evaluate(
    cases: list[dict],
    retriever: HybridRetriever,
    *,
    top_k: int,
    mode: str,
    pipeline: RagPipeline | None = None,
) -> dict:
    per_case: list[dict] = []
    recalls: list[float] = []
    mrrs: list[float] = []
    answerable = 0
    refusals: list[bool] = []
    coverages: list[bool] = []
    citations: list[float] = []
    no_citation = 0

    for case in cases:
        hits = retriever.retrieve(
            case["query"], top_k=top_k, filters={"audience": "public"}, mode=mode
        )
        predicted = [hit.chunk_id for hit in hits]

        if case["answerable"]:
            answerable += 1
            # 可回答题才计入 Recall / MRR 的分母
            recall = recall_at_k(predicted, case["expected_chunk_ids"], k=top_k)
            reciprocal = mrr(predicted, case["expected_chunk_ids"], k=top_k)
            recalls.append(recall)
            mrrs.append(reciprocal)
        else:
            # 不可回答题没有标准出处，不参与检索指标；只记录是否检索到了内容
            recall = None
            reciprocal = None

        row = {
            "case_id": case["case_id"],
            "query": case["query"],
            "type": case["type"],
            "answerable": case["answerable"],
            "expected": case["expected_chunk_ids"],
            "predicted": predicted,
            "recall": recall,
            "mrr": reciprocal,
        }

        if pipeline is not None:
            generated = _generate_case(pipeline, case["case_id"], case["query"], top_k)
            row.update(generated)

            if generated["error"] is None:
                if case["answerable"]:
                    coverages.append(not generated["refused"])
                    if not generated["refused"]:
                        citations.append(generated["citation_accuracy"])
                        if not generated["cited"]:
                            no_citation += 1
                else:
                    refusals.append(generated["refused"])

        per_case.append(row)

    hit_rate = (
        sum(1 for value in recalls if value > 0) / len(recalls) if recalls else 0.0
    )

    metrics = {
        "recall_at_k": round(statistics.fmean(recalls), 4) if recalls else 0.0,
        "mrr": round(statistics.fmean(mrrs), 4) if mrrs else 0.0,
        "hit_rate": round(hit_rate, 4),
        "refusal_accuracy": None,
        "answer_coverage": None,
        "citation_accuracy": None,
    }
    counts = {
        "cases": len(cases),
        "answerable": answerable,
        "unanswerable": len(cases) - answerable,
    }

    if pipeline is not None:
        # 拒答率只看不可回答题，覆盖率只看可回答题，两者必须成对出现，
        # 否则"全拒答"也能拿满分（见 metrics.refusal_accuracy 的说明）。
        metrics["refusal_accuracy"] = (
            round(statistics.fmean([float(ok) for ok in refusals]), 4) if refusals else None
        )
        metrics["answer_coverage"] = (
            round(statistics.fmean([float(ok) for ok in coverages]), 4) if coverages else None
        )
        metrics["citation_accuracy"] = (
            round(statistics.fmean(citations), 4) if citations else None
        )
        counts["generation_errors"] = sum(1 for row in per_case if row["error"] is not None)
        counts["answers_without_citation"] = no_citation

    return {"metrics": metrics, "counts": counts, "cases": per_case}


def conditions_mismatch(base: dict, cur: dict) -> list[str]:
    """运行条件不一致就没法比较：分数只在同条件下有意义。"""
    reasons: list[str] = []
    for key in ("mock", "mode", "top_k", "suite", "corpus", "corpus_chunks"):
        if key not in base or key not in cur:
            reasons.append(f"{key} 缺失")
        elif base[key] != cur[key]:
            reasons.append(f"{key} 不同（基线 {base[key]}，当前 {cur[key]}）")
    return reasons


def compare_reports(
    base: dict, cur: dict, *, threshold: float = 0.02
) -> tuple[list[dict], list[str], list[str]]:
    """逐条对比两次评测结果（纯函数，便于单测）。

    返回（逐条差异, 失败原因, 提示）。
    判定规则与 ci_gate 保持一致：
    1. 任一总分指标退化超过阈值 -> 失败；
    2. 单条从"有命中"变为"完全未命中" -> 失败；
    3. 其他单条下降只提示，不阻断。
    """
    base_map = {case["case_id"]: case for case in base.get("cases", [])}
    rows: list[dict] = []

    for case in cur.get("cases", []):
        if not case["answerable"] or case.get("recall") is None:
            continue
        old = base_map.get(case["case_id"])
        if old is None or old.get("recall") is None:
            continue
        delta = case["recall"] - old["recall"]
        status = "better" if delta > 1e-9 else "worse" if delta < -1e-9 else "same"
        rows.append(
            {
                "case_id": case["case_id"],
                "type": case["type"],
                "query": case["query"],
                "old": old["recall"],
                "new": case["recall"],
                "delta": round(delta, 4),
                "status": status,
            }
        )

    failures: list[str] = []
    hints: list[str] = []

    for key in ("recall_at_k", "mrr", "hit_rate"):
        old = base.get("metrics", {}).get(key)
        new = cur.get("metrics", {}).get(key)
        if old is None or new is None:
            continue
        if new - old < -threshold:
            failures.append(f"{key} 退化 {new - old:+.4f}，超过阈值 {-threshold:.4f}")

    for row in rows:
        if row["status"] != "worse":
            continue
        if row["new"] == 0 and row["old"] > 0:
            failures.append(f"{row['case_id']} 从命中变为完全未命中（{row['old']:.2f} -> 0.00）")
        else:
            hints.append(
                f"{row['case_id']} 下降 {row['old']:.2f} -> {row['new']:.2f}  {row['query']}"
            )

    return rows, failures, hints


def print_comparison(rows: list[dict], failures: list[str], hints: list[str]) -> None:
    worse = [row for row in rows if row["status"] == "worse"]
    better = [row for row in rows if row["status"] == "better"]

    print("-" * 58)
    print(f"逐条对比：改善 {len(better)} / 退化 {len(worse)} / 不变 {len(rows) - len(better) - len(worse)}")
    for row in (worse + better)[:15]:
        mark = "退化" if row["status"] == "worse" else "改善"
        print(
            f"  [{mark}] {row['case_id']:<6s} {row['type']:<11s} "
            f"{row['old']:.2f} -> {row['new']:.2f}  {row['query']}"
        )
    if len(worse) + len(better) > 15:
        print(f"  ... 另有 {len(worse) + len(better) - 15} 条变化")

    for item in hints:
        print(f"提示：{item}")
    if failures:
        print("对比未通过：")
        for item in failures:
            print(f"  - {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知识库检索评测")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mock", action="store_true", help="确定性假向量，不联网，CI 用")
    group.add_argument("--real", action="store_true", help="调用真实 embedding 接口")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE, help="评测集 jsonl")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="语料 json")
    parser.add_argument("--mode", choices=MODES, default="hybrid", help="检索模式")
    parser.add_argument("--top-k", type=int, default=5, help="看前几条（必须写进报告）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="结果输出路径")
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="与已有报告逐条对比（如 eval/baseline.json）；退化超阈值返回 1",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.02, help="允许的总分退化幅度，默认 0.02"
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="接生成链路，产出拒答率/覆盖率/引用准确率（需要 LLM_API_KEY）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="只跑前 N 条用例（0=全部），用于先验证 Key 是否可用"
    )
    args = parser.parse_args(argv)

    if args.top_k < 1:
        parser.error("--top-k 必须为正整数")
    if args.limit < 0:
        parser.error("--limit 不能为负数")

    chat = None
    if args.generate:
        try:
            chat = ChatClient()
        except LLMError as exc:
            print(f"初始化 LLM 失败：{exc}", file=sys.stderr)
            print("--generate 需要 LLM_API_KEY；不接生成链路请去掉该参数。", file=sys.stderr)
            return 1

    chunks = load_corpus(args.corpus)
    cases = load_cases(args.suite)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("评测集为空。", file=sys.stderr)
        return 1

    started = time.perf_counter()
    retriever, embed = build_retriever(args.mock, chunks)
    pipeline = None
    if chat is not None:
        pipeline = RagPipeline(
            chat_client=chat,
            embed_client=embed,
            store=None,
            bm25_index=None,
            retriever=retriever,
        )
    result = evaluate(cases, retriever, top_k=args.top_k, mode=args.mode, pipeline=pipeline)

    payload = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mock": args.mock,
        "mode": args.mode,
        "top_k": args.top_k,
        "generate": args.generate,
        "suite": str(args.suite.relative_to(ROOT)),
        "corpus": str(args.corpus.relative_to(ROOT)),
        "corpus_chunks": len(chunks),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        **result,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    metrics = result["metrics"]
    counts = result["counts"]
    print(f"== 评测结果（{args.mode} / top_k={args.top_k} / {'mock' if args.mock else 'real'}）==")
    print(f"用例 {counts['cases']} 条（可回答 {counts['answerable']}，不可回答 {counts['unanswerable']}）")
    print(f"Recall@{args.top_k}  {metrics['recall_at_k']:.4f}")
    print(f"MRR@{args.top_k}     {metrics['mrr']:.4f}")
    print(f"Hit Rate@{args.top_k} {metrics['hit_rate']:.4f}")
    if args.generate:
        def _fmt(value):
            return "null" if value is None else f"{value:.4f}"

        print(f"拒答准确率  {_fmt(metrics['refusal_accuracy'])}（不可回答题正确拒答比例）")
        print(f"答案覆盖率  {_fmt(metrics['answer_coverage'])}（可回答题给出了答案的比例）")
        print(f"引用准确率  {_fmt(metrics['citation_accuracy'])}（引用的 id 在上下文中的比例）")
        missing = counts.get("answers_without_citation", 0)
        if missing:
            print(f"无引用回答  {missing} 条（答了但没标来源，不计入引用准确率分母）")
        errors = counts.get("generation_errors", 0)
        if errors:
            print(f"生成失败    {errors} 条（未计入上述三项指标）")
    else:
        print("拒答率 / 覆盖率 / 引用准确率：null（加 --generate 才跑生成链路）")
    print(f"已写入 {args.out}")

    if args.mock:
        print("提示：mock 模式下的向量是哈希假向量，分数只能用于回归对比，不代表真实质量。")

    exit_code = 0
    if args.compare is not None:
        if not args.compare.exists():
            print(f"找不到对比基线：{args.compare}", file=sys.stderr)
            exit_code = 2
        else:
            base = json.loads(args.compare.read_text(encoding="utf-8"))
            reasons = conditions_mismatch(base, payload)
            if reasons:
                print("无法对比（运行条件不一致）：", file=sys.stderr)
                for reason in reasons:
                    print(f"  - {reason}", file=sys.stderr)
                exit_code = 2
            else:
                rows, failures, hints = compare_reports(
                    base, payload, threshold=args.threshold
                )
                print_comparison(rows, failures, hints)
                if failures:
                    exit_code = 1

    for obj in (embed, chat):
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
