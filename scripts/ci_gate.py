#!/usr/bin/env python3
"""评测门禁：把本次结果与基线比较，退化超阈值就失败。

用法：
    py -3.13 scripts/ci_gate.py
    py -3.13 scripts/ci_gate.py --baseline eval/baseline.json --threshold 0.02

退出码：
    0  通过
    1  指标退化，应阻断合入
    2  无法比较（条件不一致或文件缺失）

设计要点：运行条件（mode / top_k / mock / 语料）一律从基线读取，
不接受命令行覆盖，避免"拿不同条件的结果强行对比"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_eval

DEFAULT_BASELINE = ROOT / "eval" / "baseline.json"

# 被监控的指标；值为 None 的指标（依赖生成链路）自动跳过
WATCHED = ("recall_at_k", "mrr", "hit_rate")


def load_baseline(path: Path) -> dict:
    if not path.exists():
        print(f"找不到基线文件：{path}", file=sys.stderr)
        print("请先运行：py -3.13 scripts/run_eval.py --mock --out eval/baseline.json", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def check_conditions(base: dict) -> None:
    """条件不一致就拒绝比较。分数只有在同条件下才有意义。"""
    required = ("mode", "top_k", "mock", "corpus", "suite", "corpus_chunks")
    missing = [key for key in required if key not in base]
    if missing:
        print(f"基线缺少复现条件字段：{missing}", file=sys.stderr)
        raise SystemExit(2)


def diff_cases(base_cases: list[dict], cur_cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """逐条比较，返回（退化的用例，改善的用例）。

    只比较可回答题的 Recall —— 它最能说明"这条题从能找到变成找不到了"。
    """
    base_map = {c["case_id"]: c for c in base_cases}
    regressions: list[dict] = []
    improvements: list[dict] = []

    for cur in cur_cases:
        if not cur["answerable"]:
            continue
        old = base_map.get(cur["case_id"])
        if old is None or old["recall"] is None or cur["recall"] is None:
            continue
        delta = cur["recall"] - old["recall"]
        if delta < -1e-9:
            regressions.append({"case": cur, "old": old["recall"], "new": cur["recall"]})
        elif delta > 1e-9:
            improvements.append({"case": cur, "old": old["recall"], "new": cur["recall"]})

    return regressions, improvements


def decide_gate(
    base_metrics: dict,
    cur_metrics: dict,
    regressions: list[dict],
    threshold: float,
) -> tuple[list[str], list[str]]:
    """门禁判定（纯函数，便于单测）。

    两条规则：
    1. 任一总分指标退化超过阈值 -> 失败
    2. 任一用例从"有命中"变成"完全未命中" -> 失败

    其他单条下降只记为警告：平均分可能掩盖长尾，但单条部分下降
    未必是缺陷（例如多跳题换了一种合理排序）。
    """
    failures: list[str] = []
    warnings: list[str] = []

    for key in WATCHED:
        old = base_metrics.get(key)
        new = cur_metrics.get(key)
        if old is None or new is None:
            continue
        delta = new - old
        if delta < -threshold:
            failures.append(f"{key} 退化 {delta:+.4f}，超过阈值 {-threshold:.4f}")

    for item in regressions:
        case = item["case"]
        if item["new"] == 0 and item["old"] > 0:
            failures.append(
                f"{case['case_id']} 从命中变为完全未命中（{item['old']:.2f} -> 0.00）"
            )
        else:
            warnings.append(
                f"{case['case_id']} 下降 {item['old']:.2f} -> {item['new']:.2f}  {case['query']}"
            )

    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测门禁")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.02,
        help="允许的最大退化幅度（绝对值），默认 0.02",
    )
    args = parser.parse_args(argv)

    base = load_baseline(args.baseline)
    check_conditions(base)

    # 完全按基线记录的条件重跑，杜绝条件漂移
    chunks = run_eval.load_corpus(ROOT / base["corpus"])
    cases = run_eval.load_cases(ROOT / base["suite"])

    if len(chunks) != base["corpus_chunks"]:
        print(
            f"语料数量变了：基线 {base['corpus_chunks']}，当前 {len(chunks)}。"
            "语料变更需要重新生成基线。",
            file=sys.stderr,
        )
        return 2

    retriever, embed = run_eval.build_retriever(base["mock"], chunks)
    result = run_eval.evaluate(cases, retriever, top_k=base["top_k"], mode=base["mode"])

    print(
        f"条件：mode={base['mode']} top_k={base['top_k']} "
        f"mock={base['mock']} corpus_chunks={base['corpus_chunks']}"
    )
    print(f"门禁阈值：退化不超过 {args.threshold:.4f}")
    print("-" * 58)

    regressions, improvements = diff_cases(base["cases"], result["cases"])
    failures, warnings = decide_gate(
        base["metrics"], result["metrics"], regressions, args.threshold
    )

    for key in WATCHED:
        old = base["metrics"].get(key)
        new = result["metrics"].get(key)
        if old is None or new is None:
            print(f"{key:14s} 跳过（基线未记录）")
            continue
        delta = new - old
        status = "FAIL" if delta < -args.threshold else "PASS"
        print(f"{key:14s} {old:.4f} -> {new:.4f}  ({delta:+.4f})  {status}")

    print("-" * 58)
    print(f"逐条变化：退化 {len(regressions)} 条，改善 {len(improvements)} 条")

    for item in regressions[:10]:
        case = item["case"]
        print(
            f"  [退化] {case['case_id']} {case['type']:12s} "
            f"{item['old']:.2f} -> {item['new']:.2f}  {case['query']}"
        )
    if len(regressions) > 10:
        print(f"  ... 另有 {len(regressions) - 10} 条")

    close = getattr(embed, "close", None)
    if callable(close):
        close()

    print("-" * 58)
    for item in warnings:
        print(f"警告：{item}")
    if failures:
        print("门禁未通过：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("门禁通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
