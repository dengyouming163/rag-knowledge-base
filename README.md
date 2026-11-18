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

## 后续计划

- Day 2：文档向量化与向量库接入
- Day 3：BM25 + 混合检索与融合排序
- Day 4：Rerank 与引用溯源
- Day 5：评测集与自动化指标
- Day 6-7：服务化与面试演示材料
