#!/usr/bin/env python3
"""端到端 RAG 演示：切分 → 向量化 → 入库 → 混合检索 → 多轮改写 → 生成回答。

无 Key 演示（确定性假向量 + 固定假 LLM，不联网）：
    py -3.13 scripts/demo_pipeline.py --mock

接入真实模型（需要 EMBED_API_KEY 与 LLM_API_KEY，参考 .env.example）：
    py -3.13 scripts/demo_pipeline.py --real
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compare_routes import HashEmbeddingClient
from src.bm25 import BM25Index
from src.chunk import split_document
from src.embed import EmbeddingClient, EmbeddingError
from src.llm import ChatClient, ChatResult, LLMError
from src.pipeline import RagPipeline
from src.retrieve import HybridRetriever
from src.store import LocalVectorStore

DOCS_DIR = ROOT / "data" / "docs"

BUILTIN_DOCS: list[tuple[str, str]] = [
    (
        "kb-after-sale",
        """# 售后与退款政策

## 退款条件
签收后七天内支持无理由退款，商品需保持完好、配件齐全。定制类商品与非质量问题拆封的个护用品不支持无理由退款。

## 退款到账
退款审核通过后原路退回：微信/支付宝通常 1-3 个工作日到账，银行卡 3-7 个工作日到账。超时未到账请提供订单号联系客服人工核查。
""",
    ),
    (
        "kb-logistics",
        """# 物流与配送

## 发货时效
现货商品在付款后 24 小时内发出，预售商品以商品页标注的发货时间为准。节假日顺延。

## 运费说明
单笔订单满 99 元包邮，未满收取 8 元运费；偏远地区（新疆、西藏等）统一收取 15 元。
""",
    ),
    (
        "kb-warranty",
        """# 保修与维修

## 保修期限
整机保修一年、主要部件保修两年，以发票日期起算。人为损坏、进水与私自拆修不在保修范围内。

## 报修流程
在 App「我的-售后服务」提交报修单，上传故障视频与购买凭证，审核通过后安排寄修或上门。
""",
    ),
]

HISTORY: list[dict] = [
    {"role": "user", "content": "你们的退款政策是什么？"},
    {"role": "assistant", "content": "签收后七天内支持无理由退款，商品需保持完好。"},
]


class FakeChatClient:
    """固定返回：改写请求返回预设问题，生成请求返回预设回答。"""

    def __init__(self, rewritten: str, answer: str) -> None:
        self.rewritten = rewritten
        self.answer = answer

    def chat(
        self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 512
    ) -> ChatResult:
        prompt = messages[-1]["content"]
        text = self.rewritten if "改写" in prompt else self.answer
        return ChatResult(
            text=text,
            prompt_tokens=len(prompt),
            completion_tokens=len(text),
            latency_ms=0.0,
            model="fake",
        )

    def close(self) -> None:
        pass


def load_documents() -> tuple[list[tuple[str, str]], str]:
    """优先读取 data/docs/*.md；目录不存在或为空时用内置三段示例。"""
    files = sorted(DOCS_DIR.glob("*.md")) if DOCS_DIR.is_dir() else []
    if not files:
        return BUILTIN_DOCS, "内置示例（data/docs 为空）"
    return [(path.stem, path.read_text(encoding="utf-8")) for path in files], "data/docs"


def build_clients(mock: bool) -> tuple[object, object]:
    if mock:
        embed = HashEmbeddingClient(dim=64)
        chat = FakeChatClient(
            rewritten="退款几天到账",
            answer="退款审核通过后原路退回：微信/支付宝 1-3 个工作日，银行卡 3-7 个工作日。[kb-after-sale#1]",
        )
        return embed, chat

    try:
        embed = EmbeddingClient()
    except EmbeddingError as exc:
        print(f"初始化 Embedding 失败：{exc}", file=sys.stderr)
        print("请设置 EMBED_API_KEY 后重试，或改用 --mock 无 Key 演示。", file=sys.stderr)
        raise SystemExit(1)
    try:
        chat = ChatClient()
    except LLMError as exc:
        print(f"初始化 LLM 失败：{exc}", file=sys.stderr)
        print("请设置 LLM_API_KEY 后重试，或改用 --mock 无 Key 演示。", file=sys.stderr)
        raise SystemExit(1)
    return embed, chat


def report_step(label: str, detail: str, started: float) -> None:
    print(f"[{label}] {detail}（耗时 {(time.perf_counter() - started) * 1000:.1f} ms）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端 RAG 链路演示")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mock", action="store_true", help="确定性假向量 + 假 LLM，不联网")
    group.add_argument("--real", action="store_true", help="调用真实 SiliconFlow embedding 与 ChatClient")
    parser.add_argument("--query", default="那要几天到账？", help="用户本轮追问")
    parser.add_argument("--top-k", type=int, default=3, help="最终送给模型的资料条数")
    parser.add_argument("--audience", default="public", help="audience 过滤值")
    parser.add_argument("--min-score", type=float, default=0.0, help="最高分低于此值走兜底")
    args = parser.parse_args(argv)

    mode = "mock" if args.mock else "real"
    print(f"== RAG 端到端演示（{mode} 模式）==")

    documents, source = load_documents()
    started = time.perf_counter()
    chunks = []
    for doc_id, text in documents:
        chunks.extend(split_document(text, doc_id=doc_id, title=doc_id))
    report_step("1 切分", f"{len(documents)} 篇文档 → {len(chunks)} 个 chunk（来源：{source}）", started)
    if not chunks:
        print("没有可检索的文档。", file=sys.stderr)
        return 1

    try:
        embed, chat = build_clients(args.mock)
    except SystemExit as exc:
        return int(exc.code or 1)

    started = time.perf_counter()
    vectors = embed.embed_texts([chunk.text for chunk in chunks])
    report_step("2 向量化", f"{len(vectors)} 条文本 → {len(vectors[0])} 维向量", started)

    store = LocalVectorStore(tempfile.mkdtemp(prefix="rag-demo-"))
    started = time.perf_counter()
    written = store.upsert(chunks, vectors)
    report_step(
        "3 入库",
        f"新增 {written} 条，去重跳过 {len(chunks) - written} 条，库内共 {store.count()} 条",
        started,
    )

    started = time.perf_counter()
    bm25 = BM25Index(chunks)
    report_step("4 建索引", f"BM25 索引 {len(bm25)} 个 chunk", started)

    retriever = HybridRetriever(store, bm25, embed)
    pipeline = RagPipeline(
        chat, embed, store, bm25, retriever, min_score=args.min_score
    )

    print(f"  用户追问：{args.query}")
    print(f"  对话历史：{len(HISTORY)} 条")
    started = time.perf_counter()
    result = pipeline.answer(
        args.query, history=HISTORY, audience=args.audience, top_k=args.top_k
    )
    report_step("5 检索+回答", f"改写后的问题：{result['rewritten']}", started)

    if result["chunks"]:
        print("  命中 chunk：")
        for chunk_id in result["chunks"]:
            chunk = bm25.get_chunk(chunk_id)
            path = " / ".join(chunk.section_path) if chunk else ""
            print(f"    - {chunk_id}  {path}")
    else:
        print("  命中 chunk：（无，已走兜底）")
    print(f"  回答：{result['answer']}")

    close = getattr(chat, "close", None)
    if callable(close):
        close()
    close = getattr(embed, "close", None)
    if callable(close):
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
