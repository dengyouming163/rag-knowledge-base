# 向量库设计

## 存储结构

每个库一个目录，内含 `vectors.json`：`version`、`dim`、`records[]`。每条记录保存 chunk_id、doc_id、text、content_hash、metadata（section_path、title、audience、source_url）与 L2 归一化后的向量。启动时载入内存，检索时矩阵点积即余弦相似度。

## 去重与写入

`content_hash` 为唯一键，已存在相同 hash 的块直接跳过，返回实际新增数。写入先把临时文件刷盘，再用 `os.replace` 原子替换，避免进程中断导致半截文件。

## 过滤

`search` 的 filters 对 metadata 做精确匹配，当前用于 audience（public/internal），为后续按权限隔离检索预留。

## 选型与局限

不使用 Chroma：规避 Python 3.13 依赖链兼容风险，且本地实现零额外依赖、易测试。局限：单机、全量载入内存、无 ANN 索引，写入需整文件重写；数据量大或需并发时替换为 Qdrant/pgvector，以获得持久化索引、ANN 检索、过滤下推与多副本能力。
