"""--generate 分支的聚合逻辑测试：全部用假组件，不联网。

重点验证三件事：
1. "模型自己说不确定"也要算正确拒答（不能只看 chunks 是否为空）；
2. 引用准确率只在实际作答的题上算均值，拒答题不进分母；
3. 单条 LLM 失败只记 error，不污染指标、也不中断整轮。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_eval

from src.llm import LLMError


class FakeRetriever:
    def retrieve(self, query, *, top_k, filters, mode):
        return [SimpleNamespace(chunk_id="c1"), SimpleNamespace(chunk_id="c2")]


class FakePipeline:
    """按 query 返回预设结果，模拟 pipeline.answer 的输出结构。"""

    def __init__(self, mapping: dict[str, dict]) -> None:
        self.mapping = mapping

    def answer(self, question: str, top_k: int = 5, **kwargs) -> dict:
        outcome = self.mapping[question]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _case(case_id: str, query: str, *, answerable: bool = True) -> dict:
    return {
        "case_id": case_id,
        "query": query,
        "type": "fact",
        "answerable": answerable,
        "expected_chunk_ids": ["c1"],
    }


def test_model_self_refusal_counts_as_refusal() -> None:
    """chunks 非空、但模型写"无法确定"，也必须算正确拒答。"""
    assert run_eval._is_refusal("根据现有资料无法确定保修期。", ["c1", "c2"]) is True
    assert run_eval._is_refusal("退款 1-3 个工作日到账 [c1]。", ["c1"]) is False
    # 空上下文一定是拒答，与措辞无关
    assert run_eval._is_refusal("……", []) is True


def test_metrics_skip_refused_cases() -> None:
    cases = [
        _case("a1", "能退吗"),
        _case("a2", "多久到账"),
        _case("u1", "有积分吗", answerable=False),
    ]
    pipeline = FakePipeline(
        {
            "能退吗": {
                "answer": "支持七天无理由退款 [c1]。",
                "chunks": ["c1", "c2"],
                "citation_accuracy": 1.0,
                "citation_ok": True,
            },
            "多久到账": {
                "answer": "根据现有资料无法确定。",
                "chunks": ["c1", "c2"],
                "citation_accuracy": 0.0,
                "citation_ok": True,
            },
            "有积分吗": {
                "answer": "根据现有资料无法确定积分规则。",
                "chunks": ["c1"],
                "citation_accuracy": 0.0,
                "citation_ok": True,
            },
        }
    )

    report = run_eval.evaluate(
        cases, FakeRetriever(), top_k=5, mode="hybrid", pipeline=pipeline
    )
    metrics = report["metrics"]

    # 可回答 2 条：1 条正常作答、1 条自己拒答
    assert metrics["answer_coverage"] == 0.5
    # 不可回答 1 条，被正确拒答
    assert metrics["refusal_accuracy"] == 1.0
    # 引用准确率只算"真正作答"的那条
    assert metrics["citation_accuracy"] == 1.0
    assert report["counts"]["generation_errors"] == 0


def test_generation_error_is_isolated() -> None:
    cases = [_case("a1", "能退吗"), _case("a2", "多久到账")]
    pipeline = FakePipeline(
        {
            "能退吗": LLMError("超时"),
            "多久到账": {
                "answer": "1-3 个工作日 [c1]。",
                "chunks": ["c1"],
                "citation_accuracy": 1.0,
                "citation_ok": True,
            },
        }
    )

    report = run_eval.evaluate(
        cases, FakeRetriever(), top_k=5, mode="hybrid", pipeline=pipeline
    )

    assert report["counts"]["generation_errors"] == 1
    # 失败那条不进入覆盖率分母，剩下的那条是"作答"
    assert report["metrics"]["answer_coverage"] == 1.0
    assert report["metrics"]["citation_accuracy"] == 1.0
