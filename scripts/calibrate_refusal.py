"""拒答阈值标定：把"要不要拒答"从拍脑袋变成一条可复现的曲线。

做法：
1. 对 50 条评测题各取「向量路 top1 余弦相似度」作为"证据强度"；
2. 在 0.00–0.90 之间扫描阈值 t：
   - 可回答题 max_sim ≥ t → 判为"作答"（正确）
   - 不可回答题 max_sim < t → 判为"拒答"（正确）
3. 输出准确率曲线与最优阈值。

只有检索侧证据，没有调用 LLM，因此这是"拒答策略"的标定，不是端到端回答质量。

用法：
    py -3.13 scripts/calibrate_refusal.py              # mock 向量
    py -3.13 scripts/calibrate_refusal.py --real       # 真实 Embedding
    py -3.13 scripts/calibrate_refusal.py --write      # 写入 eval/refusal_calibration.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for item in (ROOT, ROOT / "scripts"):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from compare_routes import HashEmbeddingClient  # noqa: E402
from run_eval import build_retriever, load_cases, load_corpus  # noqa: E402

EVAL_DIR = ROOT / "eval"
OUT = EVAL_DIR / "refusal_calibration.json"


def collect_scores(mock: bool, mode: str = "vector") -> tuple[list[float], list[float]]:
    """返回 (可回答题的最高分列表, 不可回答题的最高分列表)。

    mode 决定分值的量纲，**两者不可混用**：
    - vector：余弦相似度，0~1；
    - hybrid：RRF 融合分，量级只有 0.01~0.04。
    所以阈值必须按 mode 分别标定。
    """
    chunks = load_corpus(EVAL_DIR / "demo_corpus.json")
    cases = load_cases(EVAL_DIR / "cases.jsonl")
    retriever, embed = build_retriever(mock, chunks)

    answerable: list[float] = []
    unanswerable: list[float] = []
    try:
        for case in cases:
            hits = retriever.retrieve(
                case["query"], top_k=1, filters={"audience": "public"}, mode=mode
            )
            top = hits[0].score if hits else 0.0
            (answerable if case["answerable"] else unanswerable).append(float(top))
    finally:
        close = getattr(embed, "close", None)
        if callable(close):
            close()
    return answerable, unanswerable


def evaluate_threshold(
    answerable: list[float], unanswerable: list[float], threshold: float
) -> dict:
    kept = sum(1 for score in answerable if score >= threshold)
    refused = sum(1 for score in unanswerable if score < threshold)
    total = len(answerable) + len(unanswerable)
    accuracy = (kept + refused) / total if total else 0.0
    return {
        "threshold": round(threshold, 2),
        "accuracy": round(accuracy, 4),
        "false_refusal": len(answerable) - kept,
        "false_answer": len(unanswerable) - refused,
    }


def sweep(
    answerable: list[float], unanswerable: list[float], step: float = 0.02
) -> list[dict]:
    """按 step 扫描阈值。不同 mode 的合理步长不同（hybrid 分差只有千分之一量级）。"""
    count = int(round(0.9 / step))
    return [
        evaluate_threshold(answerable, unanswerable, round(i * step, 4))
        for i in range(count + 1)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="使用确定性假向量（默认）")
    parser.add_argument("--real", action="store_true", help="使用真实 Embedding")
    parser.add_argument(
        "--mode", choices=["vector", "bm25", "hybrid"], default="vector",
        help="按哪种检索模式标定（分数量纲不同，阈值不可混用）",
    )
    parser.add_argument("--step", type=float, default=0.02, help="阈值扫描步长")
    parser.add_argument("--write", action="store_true", help="写入 eval/refusal_calibration.json")
    args = parser.parse_args()

    if args.real and args.mock:
        parser.error("--mock 与 --real 不能同时使用")
    if args.step <= 0:
        parser.error("--step 必须为正数")

    answerable, unanswerable = collect_scores(mock=not args.real, mode=args.mode)
    rows = sweep(answerable, unanswerable, step=args.step)
    best = max(rows, key=lambda row: (row["accuracy"], -row["threshold"]))

    print(f"检索模式 {args.mode}（{'real' if args.real else 'mock'} 向量）")
    print(f"可回答题 {len(answerable)} 条，最高分 中位数 {statistics.median(answerable):.4f}")
    print(f"不可回答 {len(unanswerable)} 条，最高分 中位数 {statistics.median(unanswerable):.4f}")
    print("\n阈值  准确率  误拒  误答")
    for row in rows[::5]:
        print(f"{row['threshold']:.4f}  {row['accuracy']:.4f}  {row['false_refusal']:>4}  {row['false_answer']:>4}")
    print(f"\n最优阈值 {best['threshold']:.4f} → 准确率 {best['accuracy']:.4f}"
          f"（误拒 {best['false_refusal']}，误答 {best['false_answer']}）")

    if args.write:
        OUT.write_text(
            json.dumps(
                {
                    "mode": args.mode,
                    "embedding": "real" if args.real else "mock",
                    "answerable_count": len(answerable),
                    "unanswerable_count": len(unanswerable),
                    "best": best,
                    "curve": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已写入 {OUT.relative_to(ROOT)}")

    return 0 if best["accuracy"] >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
