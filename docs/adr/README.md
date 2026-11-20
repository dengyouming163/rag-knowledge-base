# ADR 索引

| 编号 | 决策 | 状态 |
|---|---|---|
| [0001](0001-自研检索链路.md) | 主链路不引入 LangChain / LlamaIndex / LangGraph | 已采纳 |
| [0002](0002-pgvector-而非专用向量库.md) | 用 Postgres + pgvector，而非 Milvus/Qdrant | 已采纳 |
| [0003](0003-混合检索与-rrf.md) | 向量 + BM25 双路召回，用 RRF 融合 | 已采纳 |
| [0004](0004-父子分块.md) | 子块检索、父块补上下文 | 已采纳 |
| [0005](0005-三层隔离.md) | 强制作用域 + 文档 ACL + 数据库 RLS | 已采纳 |
| [0006](0006-版本快照回滚.md) | 发布存 chunk_id 快照，回滚切指针 | 已采纳 |
| [0007](0007-async-ingest.md) | PostgreSQL 任务表异步入库，单 worker 状态机与重试 | 已采纳 |
| [0008](0008-ocr-fallback.md) | parser 优先、本地 pypdf 降级，仅解析块列表为空时 OCR | 已采纳 |
| [0009](0009-ops-cockpit.md) | 调测驾驶舱用单文件三栏 shell，写动作交给白名单 ops-runner | 已采纳 |

> 格式：状态 / 背景 / 决策 / 后果 / 对应代码。新增决策请继续编号，不要改写历史 ADR。
