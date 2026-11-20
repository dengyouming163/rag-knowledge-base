# ADR 0009：调测驾驶舱用单文件三栏 shell，写动作交给白名单 ops-runner

- **状态**：已采纳
- **日期**：2026-11-20

## 背景

运维需要在同一个界面看到任务进度、Trace、AI 指标、评测/压测与身份权限，并允许管理员在受控条件下发起评测、压测和安全演练。若把这些动作直接放进业务 API 进程，评测脚本、压测流量和长任务会与在线检索/问答争抢资源，也意味着 API 需要具备执行任意命令或停启容器的能力，攻击面过大。前端若另起一套工程，又会和既有“`app/static/index.html` 单文件工作台、无构建步骤”的交付方式冲突。新增的 `ops_runs`、`ops_audit`、`ingest_job_events`、`ops_workers` 四张表也需要说明为何不套用 chunks 的 RLS。

## 决策

- 前端继续使用单个 `app/static/index.html`，在既有左侧导航外加上“中栏列表 + 右栏内容”三栏 shell；沿用微信桌面版的配色、圆角、字号与间距 token（`--wx-*`），不引入前端框架、构建链或第二个 HTML 入口。四个功能域分别是：只读驾驶舱、评测与压测、身份与权限、操作审计。
- 写动作全部交给独立进程 `scripts/run_ops.py`。它用 `claim_next(kind=...)` 领取 `ops_runs` 队列，只通过 `build_command` 的五种 kind（`eval_mock`、`loadtest`、`drill_parser`、`drill_llm`、`drill_queue`）拼装固定命令，`shell=False`，每个 kind 有固定超时；接口只接受 kind 与受限 params，因此 ops-runner 不执行任意命令，也不接受用户传入的命令行。
- 安全演练只运行隔离测试：`drill_parser` 跑 parser/OCR 相关 pytest，`drill_llm` 跑 LLM 失败用例，`drill_queue` 跑入库任务重试用例。**不真实停容器、不改 Compose、不操作运行中的 parser/worker**，所以演练验证的是降级与重试逻辑，而不是生产拓扑的可用性切换。
- 新表不新增 RLS：`ops_runs`、`ops_audit`、`ingest_job_events`、`ops_workers` 只存运维元数据（状态、时间、摘要、日志尾），不含文档正文或 chunk。读取路径在 SQL 里强制 `tenant_id` 过滤，写动作还要求管理员身份；它们没有“文档级可见性”语义，因此不像 chunks 那样启用行级安全。如果未来这些表承载跨租户共享数据或文档内容，必须补 RLS 或重新划界。
- 触发器默认关闭：`OPS_ENABLE_ACTIONS` 缺省为 `0`，身份接口只有在管理员且开关为 `1` 时返回 `ops.can_trigger=true`；同一 KB 同 kind 的活跃运行由唯一约束拦住并返回 409，避免重复压测叠加。审计写入是 best-effort，审计失败不回滚已经入队的运行。
- 可观测数据用包装器采集，不修改 `src/pipeline.py`：`app/observability.py` 用 `UsageRecordingChatClient` 与 `TimedRetriever` 包住既有客户端/检索器，把 token、模型与检索耗时写进 trace 和指标聚合，业务主链路保持原样。

## 后果

**好处**

- API 进程不需要执行外部脚本或停启容器，最坏情况只是入队一条运行；评测/压测/演练被隔离在独立进程，可按需启停。
- 五类 kind 的命令、超时和产物路径集中在一个白名单里，审计与运行历史可追溯；前端保持零构建、单文件交付。
- 驾驶舱复用既有 jobs/traces 数据和租户作用域，不复制业务查询逻辑；三栏 shell 与工作台共用视觉 token，不产生第二套设计系统。
- 包装器方式让指标采集可控可测，避免在核心检索/生成代码里散落计时代码。

**代价**

- 运营面板只能看到白名单内的动作；临时排障仍需人工登录执行，不能从界面下发任意命令。
- 未启动 ops-runner 或使用旧镜像时，运行会停留在 queued，界面只能提示“已入队”；按钮可用不代表任务会执行。
- 演练不真实停容器，无法证明生产环境在 parser/LLM/队列故障下的实际恢复时间，只覆盖逻辑层的降级与重试。
- 新表没有 RLS，隔离完全依赖 API 的租户过滤与管理员校验；任何绕过 API 的直连查询都可能读到跨租户外壳元数据。
- 三栏布局在窄屏需要退化为单列，导航与卡片数量增加后，前端单文件的体积和维护成本继续上升。

## 对应代码

- `app/static/index.html`（三栏 shell、驾驶舱/身份/评测/审计面板、`updateActionAvailability`、`triggerRun`、`renderRunMetrics`、`renderDelta`）
- `app/routers/ops.py`（摘要、运行列表与详情、入队 202/409、审计）
- `app/routers/identity.py`（身份卡、隔离层与 `ops.can_trigger`）
- `app/deps.py`（`OPS_ENABLE_ACTIONS` 默认关闭）
- `app/observability.py`（不改 `src/pipeline.py` 的 usage/耗时包装器）
- `scripts/run_ops.py`（白名单命令、固定超时、领取与落状态）
- `src/ops_runs.py`、`src/ops_audit.py`、`src/ops_metrics.py`（运行记录、审计与聚合）
- `migrations/versions/0010_ops_cockpit.py`（四张运维表与 kind 约束）
- `tests/test_ops_runs.py`、`tests/test_ops_actions_api.py`、`tests/test_ops_audit_api.py`、`tests/test_identity_api.py`、`tests/test_identity_compare.py`、`tests/test_api_ui.py`
