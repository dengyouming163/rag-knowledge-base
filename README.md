# rag-knowledge-base

面向多行业客服/陪伴场景的知识库问答（RAG）项目，用于展示检索增强生成链路的工程实现。

## 环境要求

本机 `python` 默认指向 2.7，必须显式使用 3.13：

```bash
py -3.13 -m pip install httpx pydantic pytest
py -3.13 -m pytest -q
```

## 当前进度

- Day 1：项目骨架、可配置的 LLM 对话客户端（httpx + pydantic），支持指数退避重试与延迟/token 统计；Markdown 结构化切分（标题层级/段落/超长段二次切分/重叠/稳定 chunk_id），均配套单测。
- Day 2：Embedding 客户端（SiliconFlow bge-m3，分批请求、指数退避、token 用量统计）与本地向量库（numpy + JSON，content_hash 去重、余弦检索、audience 过滤、原子落盘）。选型说明：不引入 Chroma 以规避 Python 3.13 依赖兼容风险，本地实现零额外依赖、便于测试；生产数据量大时替换为 Qdrant/pgvector（ANN 索引、并发与过滤下推）。设计细节见 `docs/vector-store-design.md`。
- Day 2 补充：混合检索。自研零依赖 BM25（中文按单字 + 相邻双字 bigram，英文/数字按 `\w+` 小写切分；倒排表 + 标准 BM25 打分，k1=1.5、b=0.75），与向量检索各取 `recall_k` 条后用 RRF 融合：`score = Σ 1/(RRF_K + rank)`，`RRF_K = 60`（沿用原论文取值：让头部名次的分差足够平缓，避免单路第一名碾压另一路的中等结果，且无需把余弦相似度与 BM25 分数归一到同一量纲）。支持 `hybrid/vector/bm25` 三种模式与 `audience` 过滤，三路对比脚本见 `docs/retrieval-comparison.md`。

## 后续计划

- Day 3：Rerank 与引用溯源
- Day 4：评测集与自动化指标
- Day 5-6：服务化与面试演示材料
