# ADR 0007：用 PostgreSQL 任务表驱动异步入库

- **状态**：已采纳
- **日期**：2026-11-19

## 背景

PDF 解析、OCR、批量 Embedding 和索引重建都可能耗时数十秒，并依赖外部服务。若在上传请求内同步完成，容易造成请求超时，也无法给前端可靠的进度与失败状态。上传、本地目录和飞书连接器还需要复用同一套去重、入库与重试逻辑，但当前项目不希望为任务队列额外维护 Redis / Celery。

## 决策

- 上传 API 负责文件接收、`content_hash` 去重、文档记录、任务入队和落盘，完成后返回 `202 + job_id + doc_id`；解析、切分、向量化不在请求内执行。
- `ingest_jobs` 作为唯一队列，状态为 `queued / running / succeeded / failed`。同一文档已有 queued/running 任务时复用任务，事务级咨询锁防止并发入队产生重复任务。
- worker 按 id 选择最早任务，以 `FOR UPDATE SKIP LOCKED` 原子领取并转为 running；每轮最多处理一条。任务记录 `attempts / progress / error / finished_at`，普通异常重新排队，最多三次尝试；不可重试异常或达到上限后转为 failed。
- worker 成功时完成解析、父子分块、子块 Embedding、chunks 写入和 BM25 重建，再把任务置为 succeeded、文档置为 ready；重试期间文档保持 pending，只有终态失败才把文档置为 failed。
- 本地目录与飞书连接器由 worker 进程定时同步，通过 `receive_source_document` 复用相同的落盘、去重和入队路径。
- 不引入独立消息中间件。当前 Compose 只运行一个 worker；API 与 worker 通过 `uploads` named volume 共享 `data/uploads`。

## 后果

**好处**

- 上传请求快速返回，前端可以轮询任务状态、来源、进度、尝试次数和错误，而不是等待整个入库过程。
- 队列、业务数据与租户作用域都由 PostgreSQL 管理，部署组件少；`SKIP LOCKED` 避免并发领取同一任务。
- 上传与连接器共用去重、状态机和入库执行器；同一文档的 queued/running 活动任务会复用，避免并发入队堆积。
- 单个文档失败被任务边界捕获，不会直接拖垮 worker 主循环。

**代价**

- 空队列轮询会产生数据库查询；当前重试直接回到 queued，没有独立延迟队列或退避调度，高负载时需要另行治理。
- API 与 worker 不是无状态地通过队列传递文件，而是共享文件系统；跨节点部署必须提供一致的上传存储，不能只扩容器。
- 当前 Compose 明确限定单 worker，吞吐受串行处理限制。多副本虽然能靠 `SKIP LOCKED` 避免重复领取，但并发重建同一 KB 的 BM25 快照存在旧快照后写风险，解决前不能直接扩副本。
- 任务没有租约、心跳或 running 超时回收；worker 在领取后崩溃会留下 running 任务，需要运维介入，不能把它当作完整的崩溃自动恢复队列。
- 数据库记录与文件落盘不是一个事务，必须保留现有的失败状态处理与运维检查，不能只凭 queued 状态断定文件已可用。

## 对应代码

- `app/routers/documents.py`（上传、去重、202 响应与共享路径）
- `app/routers/jobs.py`（租户内任务查询）
- `src/ingest_jobs.py`（入队、SKIP LOCKED 领取、状态与重试、连接器接收入口）
- `src/worker.py`（单任务执行与文档终态同步）
- `src/ingest.py`（解析到索引重建的入库流程）
- `scripts/run_worker.py`（轮询 worker 与连接器定时同步）
- `migrations/versions/0001_init.py`（ingest_jobs 表）
- `migrations/versions/0009_ingest_job_fields.py`（source / attempts / progress / finished_at）
- `deploy/docker-compose.yml`（单 worker、parser profile 与共享 uploads volume）
- `tests/test_ingest_jobs.py`、`tests/test_api_jobs.py`、`tests/test_worker_connectors.py`
