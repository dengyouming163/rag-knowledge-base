"""src/metrics.py 的单元测试：只做纯计算，不依赖网络、数据库或模型。

测试数据刻意用"手算一遍"那组例子，保证代码结果和你纸上算的一致。
"""

from __future__ import annotations

import pytest

from src.metrics import (
    answer_coverage,
    citation_accuracy,
    mrr,
    recall_at_k,
    refusal_accuracy,
)

# ---------- Recall@k ----------


def test_recall_hits_at_rank_two():
    # 标 1 个，第 2 名命中 -> 1/1
    assert recall_at_k(["x", "kb-after-sale#1", "y"], ["kb-after-sale#1"], k=5) == 1.0


def test_recall_misses_entirely():
    # 标 1 个，前 5 名里没有 -> 0
    assert recall_at_k(["a", "b", "c"], ["z"], k=5) == 0.0


def test_recall_multi_gold_partial_credit():
    # 标 2 个，只命中 1 个 -> 0.5（这是本项目的口径）
    predicted = ["kb-after-sale#2", "x", "y"]
    gold = ["kb-after-sale#2", "kb-device#1"]

    assert recall_at_k(predicted, gold, k=5) == 0.5


def test_recall_respects_k_cutoff():
    # 正确结果在第 3 名，k=2 时看不到 -> 0
    assert recall_at_k(["a", "b", "c"], ["c"], k=2) == 0.0


def test_recall_with_empty_gold_returns_zero():
    # 不可回答题不应参与 Recall，真传进来也返回 0 而不是报错
    assert recall_at_k(["a", "b"], [], k=5) == 0.0


def test_recall_invalid_k_raises():
    with pytest.raises(ValueError):
        recall_at_k(["a"], ["a"], k=0)


# ---------- MRR ----------


def test_mrr_first_hit_at_rank_one():
    assert mrr(["kb-device#1", "x"], ["kb-device#1"], k=5) == 1.0


def test_mrr_first_hit_at_rank_two():
    assert mrr(["x", "kb-device#1"], ["kb-device#1"], k=5) == 0.5


def test_mrr_uses_first_relevant_rank_for_multi_gold():
    # 标 2 个，第 1 名和第 4 名各命中一个；MRR 只看第一个 -> 1/1
    predicted = ["kb-after-sale#2", "x", "y", "kb-device#1"]
    gold = ["kb-after-sale#2", "kb-device#1"]

    assert mrr(predicted, gold, k=5) == 1.0


def test_mrr_zero_when_not_in_top_k():
    assert mrr(["a", "b", "c"], ["c"], k=2) == 0.0


def test_mrr_empty_gold_returns_zero():
    assert mrr(["a"], [], k=5) == 0.0


# ---------- 拒答 / 覆盖率 ----------


def test_refusal_accuracy_half():
    # 2 条正确拒答、2 条编了答案
    assert refusal_accuracy([True, True, False, False]) == 0.5


def test_refusal_accuracy_empty_returns_zero():
    assert refusal_accuracy([]) == 0.0


def test_answer_coverage_three_of_four():
    assert answer_coverage([True, True, True, False]) == 0.75


def test_answer_coverage_empty_returns_zero():
    assert answer_coverage([]) == 0.0


# ---------- 引用准确率 ----------


def test_citation_accuracy_half_invalid():
    # 引用了 c1、c9，其中 c9 不在本次上下文里
    assert citation_accuracy(["c1", "c9"], {"c1", "c2"}) == 0.5


def test_citation_accuracy_all_valid():
    assert citation_accuracy(["c1", "c2"], {"c1", "c2"}) == 1.0


def test_citation_accuracy_no_citation_returns_zero():
    # 一条都没引用，视为 0；调用方另判"是否必须引用"
    assert citation_accuracy([], {"c1"}) == 0.0
