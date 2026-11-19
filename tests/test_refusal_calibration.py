"""拒答阈值标定的纯函数测试（不连库、不调模型）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from calibrate_refusal import evaluate_threshold, sweep  # noqa: E402


def test_threshold_perfectly_separates() -> None:
    result = evaluate_threshold([0.8, 0.7], [0.2, 0.1], threshold=0.5)
    assert result["accuracy"] == 1.0
    assert result["false_refusal"] == 0
    assert result["false_answer"] == 0


def test_threshold_too_high_refuses_everything() -> None:
    result = evaluate_threshold([0.6], [0.5], threshold=0.9)
    assert result["false_refusal"] == 1
    assert result["false_answer"] == 0
    assert result["accuracy"] == 0.5


def test_sweep_is_monotonic_in_coverage() -> None:
    rows = sweep([0.9, 0.5, 0.3], [0.2, 0.1])
    assert rows[0]["threshold"] == 0.0
    assert rows[-1]["threshold"] == 0.9
    # 阈值升高，误拒不会减少
    refusals = [row["false_refusal"] for row in rows]
    assert refusals == sorted(refusals)
