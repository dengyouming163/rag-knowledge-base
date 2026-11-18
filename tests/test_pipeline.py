"""src/pipeline.py 的单元测试：假检索器 + 假 LLM，不联网、不落盘。"""

from __future__ import annotations

import pytest

from src.llm import ChatResult, LLMError
from src.pipeline import FALLBACK_ANSWER, RagPipeline
from src.store import SearchHit

REWRITTEN = "退款到账需要几个工作日？"


class FakeChatClient:
    """按提示词内容区分改写与生成：改写请求返回 REWRITTEN，生成请求返回固定答案。"""

    def __init__(self, answer: str = "退款三个工作日到账。[kb#2]") -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 512) -> ChatResult:
        prompt = messages[-1]["content"]
        self.calls.append({"prompt": prompt, "temperature": temperature, "max_tokens": max_tokens})
        text = REWRITTEN if "改写" in prompt else self.answer
        return ChatResult(
            text=text,
            prompt_tokens=20,
            completion_tokens=10,
            latency_ms=1.0,
            model="fake",
        )

    @property
    def generation_calls(self) -> list[dict]:
        return [call for call in self.calls if "改写" not in call["prompt"]]


class FakeRetriever:
    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict] = []

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        recall_k: int = 40,
        filters: dict | None = None,
        mode: str = "hybrid",
    ) -> list[SearchHit]:
        self.calls.append({"query": query, "top_k": top_k, "filters": filters})
        return list(self.hits)


def make_hit(chunk_id: str, score: float = 0.9, audience: str = "public") -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        text=f"{chunk_id} 的正文",
        score=score,
        metadata={"audience": audience, "section_path": ["售后", "退款"], "title": "退款政策"},
    )


def build_pipeline(hits: list[SearchHit], **kwargs) -> tuple[RagPipeline, FakeChatClient, FakeRetriever]:
    chat = FakeChatClient()
    retriever = FakeRetriever(hits)
    pipeline = RagPipeline(
        chat,
        embed_client=object(),
        store=object(),
        bm25_index=object(),
        retriever=retriever,
        **kwargs,
    )
    return pipeline, chat, retriever


HISTORY = [
    {"role": "user", "content": "退款政策是什么？"},
    {"role": "assistant", "content": "签收七天内可无理由退款。"},
]


def test_answer_returns_text_chunk_ids_and_rewritten():
    pipeline, chat, retriever = build_pipeline([make_hit("kb#2"), make_hit("kb#1", score=0.5)])

    result = pipeline.answer("那要多久", history=HISTORY, top_k=2)

    assert result["answer"] == "退款三个工作日到账。[kb#2]"
    assert result["chunks"] == ["kb#2", "kb#1"]
    assert result["rewritten"] == REWRITTEN
    assert retriever.calls[0]["query"] == REWRITTEN
    assert len(chat.generation_calls) == 1
    generation_prompt = chat.generation_calls[0]["prompt"]
    assert "kb#2 的正文" in generation_prompt
    assert "售后 / 退款" in generation_prompt


def test_audience_filter_and_top_k_are_forwarded():
    pipeline, _, retriever = build_pipeline([make_hit("kb#2")])

    pipeline.answer("退款到账要多久", audience="internal", top_k=3)

    assert retriever.calls[0]["filters"] == {"audience": "internal"}
    assert retriever.calls[0]["top_k"] == 3


def test_empty_retrieval_falls_back_without_generation():
    pipeline, chat, _ = build_pipeline([])

    result = pipeline.answer("退款到账要多久")

    assert result["answer"] == FALLBACK_ANSWER
    assert result["chunks"] == []
    assert result["rewritten"] == "退款到账要多久"
    assert chat.calls == []


def test_low_score_below_min_score_falls_back_without_generation():
    pipeline, chat, _ = build_pipeline([make_hit("kb#2", score=0.2)], min_score=0.5)

    result = pipeline.answer("退款到账要多久")

    assert result["answer"] == FALLBACK_ANSWER
    assert result["chunks"] == []
    assert chat.generation_calls == []


def test_generation_error_propagates():
    class BrokenChat(FakeChatClient):
        def chat(self, messages, temperature=0.0, max_tokens=512):
            raise LLMError("生成失败", status_code=500)

    pipeline = RagPipeline(
        BrokenChat(),
        embed_client=object(),
        store=object(),
        bm25_index=object(),
        retriever=FakeRetriever([make_hit("kb#2")]),
    )

    with pytest.raises(LLMError):
        pipeline.answer("退款到账要多久")
