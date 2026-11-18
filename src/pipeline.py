"""端到端 RAG 流水线：问题改写 → 过滤检索 → 拼上下文 → 生成回答。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .rewrite import rewrite_query
from .store import SearchHit

_PROMPT_PATH = Path(__file__).with_name("prompts") / "answer.txt"
ANSWER_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

FALLBACK_ANSWER = (
    "抱歉，知识库中没有找到足够相关的资料，暂时无法回答这个问题。"
    "建议补充更具体的信息，或转人工客服。"
)


class RagPipeline:
    """把改写、检索、生成串成一条链路；组件全部注入，便于替换与单测。

    min_score 是兜底阈值：最高分低于它视为"检索没找到"，直接返回兜底话术，
    不再花 token 让模型在无关资料上硬答。
    """

    def __init__(
        self,
        chat_client: Any,
        embed_client: Any,
        store: Any,
        bm25_index: Any,
        retriever: Any,
        *,
        min_score: float = 0.0,
    ) -> None:
        self.chat_client = chat_client
        self.embed_client = embed_client
        self.store = store
        self.bm25_index = bm25_index
        self.retriever = retriever
        self.min_score = min_score

    def answer(
        self,
        question: str,
        history: list[dict] | None = None,
        audience: str = "public",
        top_k: int = 5,
    ) -> dict:
        """返回 {"answer": 回答文本, "chunks": [chunk_id...], "rewritten": 检索用问题}。"""
        rewritten = rewrite_query(question, history or [], self.chat_client)
        hits = self.retriever.retrieve(
            rewritten, top_k=top_k, filters={"audience": audience}
        )
        if not hits or hits[0].score < self.min_score:
            return {"answer": FALLBACK_ANSWER, "chunks": [], "rewritten": rewritten}

        prompt = ANSWER_PROMPT.format(context=_format_context(hits), question=rewritten)
        result = self.chat_client.chat(
            [{"role": "user", "content": prompt}], temperature=0.0
        )
        return {
            "answer": result.text.strip(),
            "chunks": [hit.chunk_id for hit in hits],
            "rewritten": rewritten,
        }


def _format_context(hits: list[SearchHit]) -> str:
    """每条资料带 chunk_id 与章节路径，供模型按 [chunk_id] 标注来源。"""
    blocks: list[str] = []
    for hit in hits:
        section = " / ".join(hit.metadata.get("section_path") or [])
        header = f"[{hit.chunk_id}]"
        if section:
            header = f"{header} {section}"
        blocks.append(f"{header}\n{hit.text}")
    return "\n\n".join(blocks)
