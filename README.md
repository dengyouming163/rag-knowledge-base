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

## 后续计划

- Day 3：BM25 + 混合检索与融合排序
- Day 4：Rerank 与引用溯源
- Day 5：评测集与自动化指标
- Day 6-7：服务化与面试演示材料
