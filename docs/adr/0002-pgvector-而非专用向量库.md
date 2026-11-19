# ADR 0002：用 Postgres + pgvector，而非专用向量库

- **状态**：已采纳
- **日期**：2026-11-19

## 背景

候选方案：pgvector、Milvus、Qdrant、Pinecone、ES kNN。
本项目定位“企业内多租户知识库”，规模在百万级 chunk 以内，强隔离优先于极致 QPS。

## 决策

用 `pgvector` 扩展，向量与正文、ACL、版本信息存在同一个库、同一张表。

## 后果

**好处**

- 一致性：正文与向量同一事务，不会出现“向量删了正文还在”的孤儿记录。
- 过滤下推：`tenant_id`/`kb_id` 与 `ORDER BY embedding <=> ?` 在同一条 SQL 完成。
- 运维：备份（`pg_dump`）、监控、迁移工具全部复用，不新增组件。
- 只在 `chunks` 上开 RLS 即可获得数据库级兜底。

**代价**

- 单机上限约千万级；超过后 HNSW 内存与重建时间不可控，需换专用库。
- 高选择性过滤时 HNSW 是后过滤，可能召回不足（可用 `iterative_scan` 或部分索引缓解）。
- 超级用户会绕过 RLS，业务必须用普通角色连接（见 ADR 0005）。

**迁移触发条件**：单表 > 1 亿向量、或需要水平分片、或 P99 受限于单机 IO。

## 对应代码

- `deploy/docker-compose.yml`、`migrations/versions/0002_hnsw.py`
- `src/store_pg.py`（`ORDER BY embedding <=> ...`）
