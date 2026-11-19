"""门禁判定逻辑的单测：只测纯函数 decide_gate，不跑检索。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_gate


def _case(case_id="c001", query="示例问题"):
    return {"case_id": case_id, "type": "fact", "query": query, "answerable": True}


def test_no_change_passes():
    base = {"recall_at_k": 0.9, "mrr": 0.8, "hit_rate": 0.95}
    failures, warnings = ci_gate.decide_gate(base, dict(base), [], 0.02)
    assert failures == []
    assert warnings == []


def test_metric_drop_beyond_threshold_fails():
    base = {"recall_at_k": 0.94, "mrr": 0.86, "hit_rate": 0.95}
    cur = {"recall_at_k": 0.91, "mrr": 0.86, "hit_rate": 0.95}
    failures, _ = ci_gate.decide_gate(base, cur, [], 0.02)
    assert len(failures) == 1
    assert "recall_at_k" in failures[0]


def test_metric_drop_equal_to_threshold_passes():
    """边界：正好等于阈值不算退化（浮点用近似值避开精度问题）。"""
    base = {"recall_at_k": 0.94}
    cur = {"recall_at_k": 0.9200000001}
    failures, _ = ci_gate.decide_gate(base, cur, [], 0.02)
    assert failures == []


def test_case_full_miss_fails_even_if_average_is_flat():
    """单条从命中变成完全未命中，必须失败，不能被平均分掩盖。"""
    base = {"recall_at_k": 0.9}
    cur = {"recall_at_k": 0.9}
    regressions = [{"case": _case("c017"), "old": 1.0, "new": 0.0}]
    failures, warnings = ci_gate.decide_gate(base, cur, regressions, 0.02)
    assert len(failures) == 1
    assert "c017" in failures[0]
    assert warnings == []


def test_case_partial_drop_only_warns():
    """多跳题从 1.0 掉到 0.5 只警告，不阻断。"""
    base = {"recall_at_k": 0.9}
    cur = {"recall_at_k": 0.9}
    regressions = [{"case": _case("c030"), "old": 1.0, "new": 0.5}]
    failures, warnings = ci_gate.decide_gate(base, cur, regressions, 0.02)
    assert failures == []
    assert len(warnings) == 1
    assert "c030" in warnings[0]


def test_none_metrics_are_skipped():
    """拒答率在基线里是 null，门禁必须跳过而不是报错。"""
    base = {"recall_at_k": 0.9, "refusal_accuracy": None}
    cur = {"recall_at_k": 0.9, "refusal_accuracy": 0.0}
    failures, warnings = ci_gate.decide_gate(base, cur, [], 0.02)
    assert failures == []
    assert warnings == []
