"""Task 2.4：引用校验测试。

关注的不是"能不能抽出方括号"，而是三类真实事故：
1. 模型编造不存在的来源；
2. Markdown 链接被误认成引用（假阳性）；
3. 拒答场景本来就没有引用，不能被判成"引用错误"。
"""

from __future__ import annotations

from types import SimpleNamespace

from src.citation import extract_ids, validate_citation
from src.pipeline import RagPipeline
from src.store import SearchHit


def _hit(chunk_id: str) -> SearchHit:
    return SearchHit(chunk_id=chunk_id, doc_id="doc", text=f"正文-{chunk_id}", score=1.0, metadata={})


def test_extract_ids_dedupes_and_keeps_order() -> None:
    answer = "退款 1-3 天到账 [kb-after-sale#1]，逾期请升级 [kb-after-sale#2]，再次强调 [kb-after-sale#1]。"

    assert extract_ids(answer) == ["kb-after-sale#1", "kb-after-sale#2"]


def test_markdown_links_are_not_citations() -> None:
    answer = "详见 [退款政策](https://example.invalid/policy) 与 [kb-after-sale#1]。"

    assert extract_ids(answer) == ["kb-after-sale#1"]


def test_all_citations_valid() -> None:
    report = validate_citation("退款 1-3 天到账 [a#1]", {"a#1", "a#2"})

    assert report.ok is True
    assert report.invalid_ids == []
    assert report.accuracy == 1.0


def test_fabricated_citation_is_flagged() -> None:
    report = validate_citation("退款 1-3 天到账 [a#1]，另有说明 [a#9]", {"a#1"})

    assert report.ok is False
    assert report.invalid_ids == ["a#9"]
    assert report.accuracy == 0.5


def test_answer_without_citations_is_ok_but_zero_accuracy() -> None:
    report = validate_citation("抱歉，知识库中没有相关信息。", {"a#1"})

    # 拒答场景没有引用是正常的：不判错，但准确率记 0，便于评测区分"没引"与"引对"
    assert report.ok is True
    assert report.accuracy == 0.0


def test_empty_answer_is_ok() -> None:
    assert validate_citation("", {"a#1"}).ok is True


class _Retriever:
    def __init__(self, hits):
        self._hits = hits

    def retrieve(self, query, top_k=10, filters=None, mode="hybrid", route=None):
        return list(self._hits)


class _Chat:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages, temperature=0.0):
        return SimpleNamespace(text=self.reply)


def _pipeline(reply: str) -> RagPipeline:
    return RagPipeline(
        chat_client=_Chat(reply),
        embed_client=None,
        store=None,
        bm25_index=None,
        retriever=_Retriever([_hit("a#1")]),
    )


def test_pipeline_marks_invalid_citation() -> None:
    result = _pipeline("退款 1-3 天到账 [a#1]，规则见 [a#9]").answer("退款多久到账", top_k=1)

    assert result["citation_ok"] is False
    assert result["invalid_citations"] == ["a#9"]
    # 保留模型原文，便于线上复现"它到底编了什么"
    assert "[a#9]" in result["answer"]


def test_pipeline_marks_valid_citation() -> None:
    result = _pipeline("退款 1-3 天到账 [a#1]").answer("退款多久到账", top_k=1)

    assert result["citation_ok"] is True
    assert result["citation_accuracy"] == 1.0
