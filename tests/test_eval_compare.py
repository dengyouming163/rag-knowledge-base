"""Task 2.6：逐条评测对比测试（纯函数，不碰数据库/网络）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_eval


def _report(cases, *, recall=0.9, mrr=0.8, hit_rate=0.9) -> dict:
    return {
        "mock": True,
        "mode": "hybrid",
        "top_k": 5,
        "suite": "eval/cases.jsonl",
        "corpus": "eval/demo_corpus.json",
        "corpus_chunks": 26,
        "metrics": {"recall_at_k": recall, "mrr": mrr, "hit_rate": hit_rate},
        "cases": cases,
    }


def _case(case_id, recall, *, answerable=True) -> dict:
    return {
        "case_id": case_id,
        "type": "fact",
        "query": f"问题-{case_id}",
        "answerable": answerable,
        "expected": ["a"],
        "predicted": ["a"],
        "recall": recall,
        "mrr": recall,
    }


def test_identical_reports_have_no_changes() -> None:
    base = _report([_case("c1", 1.0), _case("c2", 0.5)])

    rows, failures, hints = run_eval.compare_reports(base, base)

    assert [row["status"] for row in rows] == ["same", "same"]
    assert failures == []
    assert hints == []


def test_improvement_is_reported_not_failed() -> None:
    base = _report([_case("c1", 0.5)])
    cur = _report([_case("c1", 1.0)], recall=0.95)

    rows, failures, _ = run_eval.compare_reports(base, cur)

    assert rows[0]["status"] == "better"
    assert rows[0]["delta"] == 0.5
    assert failures == []


def test_case_flip_to_zero_is_failure() -> None:
    base = _report([_case("c1", 1.0)])
    cur = _report([_case("c1", 0.0)])

    _, failures, _ = run_eval.compare_reports(base, cur)

    assert any("c1" in item and "完全未命中" in item for item in failures)


def test_partial_drop_is_hint_only() -> None:
    base = _report([_case("c1", 1.0)])
    cur = _report([_case("c1", 0.5)])

    _, failures, hints = run_eval.compare_reports(base, cur)

    assert failures == []
    assert any("c1" in item for item in hints)


def test_total_metric_drop_beyond_threshold_fails() -> None:
    base = _report([_case("c1", 1.0)], recall=0.93)
    cur = _report([_case("c1", 1.0)], recall=0.90)

    _, failures, _ = run_eval.compare_reports(base, cur, threshold=0.02)

    assert any("recall_at_k" in item for item in failures)

    # 阈值放宽后同一份数据应通过
    _, relaxed, _ = run_eval.compare_reports(base, cur, threshold=0.05)
    assert relaxed == []


def test_unanswerable_cases_are_skipped() -> None:
    base = _report([_case("c9", None, answerable=False)])
    cur = _report([_case("c9", None, answerable=False)])

    rows, failures, _ = run_eval.compare_reports(base, cur)

    assert rows == []
    assert failures == []


def test_conditions_mismatch_detected() -> None:
    base = _report([])
    cur = _report([])
    cur["top_k"] = 10

    reasons = run_eval.conditions_mismatch(base, cur)

    assert any("top_k" in reason for reason in reasons)

    assert run_eval.conditions_mismatch(base, base) == []


def test_missing_case_in_baseline_is_ignored() -> None:
    base = _report([_case("c1", 1.0)])
    cur = _report([_case("c1", 1.0), _case("new", 0.0)])

    rows, failures, _ = run_eval.compare_reports(base, cur)

    assert [row["case_id"] for row in rows] == ["c1"]
    assert failures == []
