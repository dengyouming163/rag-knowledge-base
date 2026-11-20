# 调测驾驶舱与权限可视化设计

- 日期：2026-11-20
- 状态：待评审（设计已口头确认）
- 范围：`/ui` 单页调测驾驶舱、身份与权限可视化、任务/评测/压测/Trace/AI 指标
- 关联项目：`rag-knowledge-base`
- 代码基线：`main` @ `beff100`

## 1. 背景

当前 `/ui` 只有六块业务功能：选库、身份预设、上传跟踪、检索、流式问答、引用展开。后端已经有 `jobs`、`traces`、`stats`、`search`、`chat/stream` 等接口，但缺少面向调测的聚合视图：

- 看不到队列全貌、任务阶段、最老等待、失败原因分布；
- 看不到 Trace 瀑布，只能按 `trace_id` 单独查；
- 评测和压测结果只落 JSON 文件，UI 无法触发、对比和历史回看；
- `ChatResult` 已有 token/model/latency，但 pipeline 只取文本，AI 指标没有进入 trace；
- 身份只有一行 scope 预览，看不到真实 role、tags、ACL 条件和 RLS 拦截原因；
- 没有操作审计，无法回答“谁在什么时候触发了重试/评测/压测”。

本项目已经具备生产化的骨架：租户隔离、ACL、RLS、版本快照、任务重试、原子发布、trace、评测和压测。本期目标是把这些能力变成**可见、可解释、可受控触发**的驾驶舱，而不是另起一套系统。

## 2. 目标与非目标

### 2.1 目标

1. 在现有 `/ui` 中增加 3 个 Tab：调测驾驶舱、身份与权限、评测与压测。
2. 实时展示任务队列、任务阶段时间线、Trace 瀑布、AI 指标、评测指标、压测分位数。
3. 展示当前身份的真实 role、tags、is_admin、可见 KB、ACL 条件、RLS 状态和拒绝原因。
4. 支持同一 query 在 guest/member/admin 三种身份下的检索对比。
5. 支持管理员受控触发：失败任务重试、mock 评测、固定参数压测、安全故障演练。
6. 所有受控动作有开关、权限校验和审计时间线。
7. 历史保留最近 20 次评测/压测运行、最近 7 天 Trace，UI 可看当前值与上一次对比。
8. 保持单文件原生 HTML/CSS/JS，不引入前端构建链和 CDN。

### 2.2 非目标

- 不做完整 RBAC 管理后台（用户/角色/标签 CRUD）。
- 不做分布式告警、Prometheus/Grafana 替代品、多机调度。
- 不通过 UI 停生产容器或执行任意 shell；故障演练只做隔离的安全演练。
- 不引入 React/Vite/Chart.js 等前端依赖。
- 不修改他人正在编辑的 dirty/untracked 文件（见 11.1）。
- 本期不做运行取消；超时由 runner 兜底，后续再加取消。
- 本期不把 `X-User-Id` 升级为真实登录体系；它是调测占位，生产接入 token 后替换。

## 3. 角色与使用场景

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| 访客/普通成员 | 看自己的身份卡、有效权限、拒绝原因；看业务工作台 | 看审计、身份对比、触发任何动作 |
| 管理员 | 看全部驾驶舱、任务、Trace、评测/压测、审计；触发受控动作 | 关闭开关后仍不能触发；不能执行任意命令 |
| 运维/开发 | 通过 ops-runner 看运行状态和输出摘要；按 runbook 处理真实停服 | UI 不提供真实停容器能力 |

当前身份仍由 `X-Tenant-Id` + `X-User-Id` 推导，角色来自数据库 `users.role`。这是调测环境的身份占位，不是生产鉴权；spec 明确把这个边界写进 UI 提示。

## 4. 信息架构

保留现有业务工作台，新增 Tab：

```text
/ui
├─ 业务工作台（现有六块）
├─ 调测驾驶舱
│  ├─ 系统健康：api / db / parser / worker 心跳
│  ├─ 队列概览：queued/running/succeeded/failed、最老等待、平均耗时、失败原因 Top
│  ├─ 任务看板：列式任务卡 + 重试按钮
│  ├─ 任务时间线：queued → claimed → parsing → chunking → embedding → publishing → ready/failed
│  ├─ AI 指标：tokens、模型、LLM 延迟、tokens/s、估算成本、引用通过率、rerank 降级率
│  └─ 最近 Trace：列表 + 瀑布
├─ 身份与权限
│  ├─ 身份卡：tenant/user/role/tags/is_admin/可见 KB
│  ├─ 隔离三层：作用域 / ACL / RLS 状态
│  ├─ 身份切换对比：guest/member/admin 同 query 检索对比
│  ├─ 拒绝原因：租户 → ACL → RLS 逐层解释
│  └─ 操作审计：actor/action/target/result/reason/时间线
└─ 评测与压测
   ├─ 评测指标卡：Recall@5/MRR@5/Hit@5/拒答准确率/覆盖率/引用准确率
   ├─ 与上次运行对比：升降箭头 + 差值
   ├─ 压测指标：search/chat p50/p95/p99、错误率、rerank 降级率
   ├─ 运行历史：最近 20 次
   └─ 受控触发：重试、mock 评测、固定参数压测、安全演练
```

## 5. 数据模型

新增 Alembic 迁移 `0010_ops_cockpit`。所有业务表都带 `tenant_id`，查询必须显式带租户条件；`ops_workers` 是全局进程表，不带租户。

### 5.1 `ops_runs`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | bigserial PK | 运行 ID |
| `tenant_id` | text not null | 租户 |
| `kb_id` | text not null | 知识库 |
| `kind` | text not null | `eval_mock` / `loadtest` / `drill_parser` / `drill_llm` / `drill_queue` |
| `status` | text not null | `queued` / `running` / `succeeded` / `failed` |
| `params` | jsonb not null default `{}` | 固定参数快照 |
| `requested_by` | text not null | 触发人 |
| `requested_at` | timestamptz not null default now() | 入队时间 |
| `started_at` | timestamptz | 开始时间 |
| `finished_at` | timestamptz | 结束时间 |
| `exit_code` | integer | 子进程退出码 |
| `summary` | jsonb | 解析后的指标摘要 |
| `artifact_path` | text | 相对仓库根的结果文件 |
| `output_tail` | text | 截断后的输出，最多 8KB |
| `error` | text | 失败原因 |
| `attempts` | integer not null default 0 | 领取次数 |

约束与索引：

- `CHECK (kind IN ('eval_mock','loadtest','drill_parser','drill_llm','drill_queue'))`。
- `CHECK (status IN ('queued','running','succeeded','failed'))`。
- 唯一部分索引：`(tenant_id, kb_id, kind) WHERE status IN ('queued','running')`，防止同类运行并发。
- 普通索引：`(tenant_id, kb_id, id DESC)`。

### 5.2 `ops_audit`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | bigserial PK | 审计 ID |
| `tenant_id` | text not null | 租户 |
| `actor` | text not null | 操作人 |
| `action` | text not null | `job_retry` / `run_eval` / `run_loadtest` / `run_drill` |
| `target` | text | job id / kb id / run id |
| `result` | text not null | `allowed` / `denied` / `succeeded` / `failed` |
| `reason` | text | 拒绝或失败原因 |
| `detail` | jsonb not null default `{}` | 非敏感参数 |
| `created_at` | timestamptz not null default now() | 时间 |

索引：`(tenant_id, id DESC)`。

审计写入时机：

- API 入队/重试时写 `allowed` 或 `denied`；
- ops-runner 完成后再写一条 `succeeded` 或 `failed`，`target=run_id`，让时间线能闭合。

### 5.3 `ingest_job_events`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | bigserial PK | 事件 ID |
| `job_id` | bigint not null references `ingest_jobs(id)` on delete cascade | 任务 |
| `tenant_id` | text not null | 租户 |
| `stage` | text not null | `queued/claimed/parsing/chunking/embedding/publishing/succeeded/failed` |
| `status` | text not null | `started/succeeded/failed` |
| `progress` | integer | 0–100 |
| `message` | text | 可读信息，不放密钥 |
| `created_at` | timestamptz not null default now() | 时间 |

索引：`(job_id, id)`。

事件写入使用**独立短事务**并 best-effort：事件写失败不能反过来让入库失败；入库失败时仍要能留下 `failed` 事件。

### 5.4 `ops_workers`

| 字段 | 类型 | 说明 |
|---|---|---|
| `worker_id` | text PK | worker 标识 |
| `last_seen` | timestamptz not null | 最近心跳 |
| `version` | text | 代码版本/启动标识 |
| `current_job_id` | bigint | 当前任务，可为空 |
| `updated_at` | timestamptz not null default now() | 更新时间 |

worker 每个轮询循环 upsert 一次；UI 用它区分“空闲”和“已死”。

### 5.5 RLS 边界

本期新表沿用应用层强制租户条件，不新增 RLS；现有 RLS 仍只覆盖 `chunks`。原因：ops 表只由管理员接口和 runner 访问，且迁移/管理连接与业务连接分离；后续硬化时再补策略。这个限制会写进 README/ADR，不在 UI 里假装已经 RLS。

## 6. API 契约

所有接口都要求 `X-Tenant-Id`；身份接口还需要 `X-User-Id`。body 一律不接受 `tenant_id`。跨租户资源返回 404。

### 6.1 身份

`GET /v1/identity`

```json
{
  "tenant": "demo",
  "user": "u_admin",
  "role": "admin",
  "tags": ["dept:cs"],
  "is_admin": true,
  "kbs": ["kb_after_sale", "kb_smoke"],
  "isolation": {
    "tenant_scope": true,
    "acl": "admin_bypass",
    "rls": {"enabled": true, "forced": true, "policy_count": 1}
  },
  "ops": {"actions_enabled": true, "can_trigger": true}
}
```

- 用户不存在时返回 `role=guest`、`tags=[]`、`is_admin=false`，不返回 500。
- `kbs` 只返回当前租户的 KB id，不返回其他租户。

### 6.2 权限解释

`GET /v1/ops/permissions?kb_id={kb_id}`

```json
{
  "tenant": "demo",
  "user": "u_member",
  "role": "member",
  "tags": ["dept:cs"],
  "is_admin": false,
  "kb": {"id": "kb_after_sale", "exists": true, "tenant_match": true},
  "acl": {"mode": "public_or_tag_intersection", "user_tags": ["dept:cs"]},
  "rls": {"enabled": true, "forced": true, "policy_count": 1, "bare_count": 0, "scoped_count": 12},
  "decision": "allow",
  "reason": "租户匹配，ACL 可见，RLS 已启用"
}
```

- `bare_count` 用不设置 `app.tenant_id` 的独立连接统计，用来证明 RLS 生效；只返回计数，不返回内容。
- `scoped_count` 统计当前租户 + 当前 ACL 可见的 ready chunks；KB 规模大时允许缓存/估算，并在响应里标注 `estimated`。
- 拒绝原因按层返回：`tenant_scope` / `kb_not_found` / `acl_empty` / `rls_blocked`。

### 6.3 驾驶舱摘要

`GET /v1/ops/summary?kb_id={kb_id}`

```json
{
  "health": {
    "api": "up",
    "db": "up",
    "parser": "up",
    "workers": [{"worker_id": "w1", "last_seen": "2026-11-20T04:51:20Z", "age_seconds": 3}]
  },
  "queue": {
    "total": 42,
    "queued": 3,
    "running": 1,
    "succeeded": 36,
    "failed": 2,
    "oldest_queued_seconds": 18,
    "avg_duration_seconds": 12.4,
    "retry_count": 5,
    "top_errors": [{"error": "parser timeout", "count": 2}]
  },
  "ai": {
    "requests": 120,
    "refusal_rate": 0.08,
    "citation_ok_rate": 0.97,
    "rerank_degraded_rate": 0.0,
    "zero_result_rate": 0.03,
    "tokens": {"prompt": 18320, "completion": 5210, "total": 23530},
    "llm_latency_ms": {"p50": 620, "p95": 1140},
    "model": "deepseek-chat",
    "estimated_cost": null
  },
  "latest_runs": [],
  "latest_traces": []
}
```

- `estimated_cost` 只有在配置 `LLM_INPUT_PRICE_PER_1K` / `LLM_OUTPUT_PRICE_PER_1K` 时才计算；否则为 `null`，UI 显示“未配置价格”。
- 指标从最近 7 天 trace 聚合；没有数据时返回 `null` 或 0，不伪造。
- `/v1/ops/traces` 只返回最近 7 天记录；本期不主动删除旧 JSONL，清理策略后续单独做。
- `parser` 状态由 `PARSER_BASE_URL/health` 探测；未配置时返回 `unconfigured`。
- worker 心跳超过 30 秒未更新标记为 `stale`。

### 6.4 任务

`GET /v1/ops/jobs?kb_id={kb_id}&status={status}&limit=50`

返回任务列表和状态计数。任务字段在现有 `jobs` 基础上增加：

- `wait_seconds`：`started_at - created_at`；
- `duration_seconds`：`finished_at - started_at`；
- `progress`、`attempts`、`source`、`error`。

`GET /v1/ops/jobs/{job_id}/events`

返回按 `id` 升序的阶段事件。跨租户返回 404。

### 6.5 Trace

`GET /v1/ops/traces?kb_id={kb_id}&limit=20`

返回最近 trace 的扁平阶段记录：

```json
{
  "traces": [
    {
      "trace_id": "abc",
      "stage": "retrieve",
      "ms": 42.1,
      "hits": 5,
      "refused": false,
      "mode": "hybrid",
      "query": "退款多久到账",
      "ts": "2026-11-20T04:51:20Z"
    }
  ]
}
```

现有 `GET /v1/traces/{trace_id}` 继续用于详情；新增字段全部可选，旧记录兼容。

### 6.6 运行历史与触发

`GET /v1/ops/runs?kb_id={kb_id}&kind={kind}&limit=20`

返回 `ops_runs` 列表，含 `summary` 和 `artifact_path`。

`POST /v1/ops/kbs/{kb_id}/runs`

```json
{"kind": "eval_mock", "params": {}}
```

- 管理员 + `OPS_ENABLE_ACTIONS=1` 才能调用。
- 返回 202 + `run_id`。
- 同类 queued/running 已存在时返回 409，并返回已有 `run_id`。
- 参数只允许白名单；服务端忽略/拒绝未知字段。
- 动作入队后写 `ops_audit(result="allowed")`；拒绝时写 `denied` 再返回 403/409。

`GET /v1/ops/runs/{run_id}`

返回状态、时间、退出码、摘要、输出尾部、错误。跨租户 404。

### 6.7 任务重试

`POST /v1/ops/jobs/{job_id}/retry`

- 仅 `failed` 可重试；否则 409。
- 重置 `status=queued`、`attempts=0`、`progress=0`、`error=''`、`finished_at=null`。
- 写 `queued` 事件（message=`manual retry`）和审计。
- 返回 202。

### 6.8 身份对比

`POST /v1/ops/kbs/{kb_id}/identity-compare`

```json
{"query": "退款多久到账", "user_ids": ["guest", "u_member", "u_admin"], "top_k": 5}
```

- 管理员 + 开关。
- 最多 3 个用户，`top_k` 1–10。
- 复用 search 服务层，不复制检索逻辑。
- 返回每个用户的 `role/tags/is_admin/hits/top_score/latency_ms/visible_chunk_ids`。
- 该接口只读，不写审计；如果后续加“以他人身份执行写操作”，必须单独审计。

### 6.9 审计

`GET /v1/ops/audit?limit=50`

管理员 + 开关。返回 `actor/action/target/result/reason/detail/created_at`，按时间倒序。跨租户只返回当前租户。

## 7. ops-runner

### 7.1 形态

新增 `scripts/run_ops.py`，作为独立进程/容器运行：

```text
ops-runner:
  循环：
    1. SELECT id, tenant_id, kb_id, kind, params
         FROM ops_runs
        WHERE status = 'queued'
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    2. 置 running、attempts+1、started_at
    3. 按 kind 查白名单命令
    4. subprocess.run(args, shell=False, timeout=timeout_seconds)
    5. 解析 JSON 摘要，写 summary/artifact_path/output_tail/exit_code
    6. 置 succeeded/failed、finished_at
```

### 7.2 命令白名单

| kind | 命令 | 超时 |
|---|---|---|
| `eval_mock` | `py -3.13 scripts/run_eval.py --mock --out data/ops/eval/{run_id}.json` | 300s |
| `loadtest` | `py -3.13 scripts/loadtest.py --concurrency 20 --requests 200 --out data/ops/loadtest/{run_id}.json` | 300s |
| `drill_parser` | `py -3.13 -m pytest tests/test_ocr_branch.py tests/test_pdf_parser.py -q` | 120s |
| `drill_llm` | `py -3.13 -m pytest tests/test_llm.py -q` | 120s |
| `drill_queue` | `py -3.13 -m pytest tests/test_ingest_jobs.py -q` | 120s |

约束：

- `shell=False`，参数固定，不拼接用户输入。
- `loadtest` 的 tenant/kb 来自 `ops_runs`，不来自任意 URL。
- 不写 `eval/baseline*.json`；结果只写 `data/ops/`，避免覆盖他人 dirty 的评测文件。
- 输出只保留尾部 8KB，避免大日志进 DB。
- 超时杀进程组，置 `failed`，`error="timeout"`。
- 同一 tenant/kb/kind 同时只允许一个 queued/running（唯一部分索引保证）。

### 7.3 安全演练边界

UI 的“故障演练”只运行隔离测试，不通过 Docker socket 停生产容器：

- `drill_parser`：验证 parser 不可用时的本地 pypdf 降级与 OCR 分支。
- `drill_llm`：验证 LLM 超时/错误的重试与可读错误。
- `drill_queue`：验证 `SKIP LOCKED` 并发领取与重试上限。

真实停服演练仍走现有 runbook；UI 会明确标注“安全演练，不影响运行中的服务”。

## 8. AI 指标采集

### 8.1 现状

`ChatResult` 已有 `prompt_tokens`、`completion_tokens`、`model`、`latency_ms`，但 `src/pipeline.py` 只取 `result.text`，usage 被丢弃。`src/pipeline.py` 是他人 dirty 文件，本期不修改。

### 8.2 采集方式

在 `app/routers/chat.py` / `app/services.py` 用包装器采集，不改 pipeline：

- `UsageRecordingChatClient`：包装 `chat_client.chat()`，记录最后一次 `ChatResult`（pipeline 先改写、再回答，最后一次是回答）。
- `TimedRetriever`：包装 `pipeline.retriever.retrieve()`，记录检索耗时。
- 构造 pipeline 后包装 `pipeline.chat_client` 和 `pipeline.retriever`；调用结束后读取包装器。
- 在 SSE 的 `hits` 事件中增加 `usage` 和 `timings` 字段；前端展示。
- Trace 的 `answer` 阶段增加可选字段：`prompt_tokens`、`completion_tokens`、`model`、`llm_ms`、`rerank_degraded`、`citation_ok`。

### 8.3 指标定义

| 指标 | 定义 | 来源 |
|---|---|---|
| `prompt_tokens` | 本次回答输入 token | `ChatResult` |
| `completion_tokens` | 本次回答输出 token | `ChatResult` |
| `total_tokens` | 输入 + 输出 | 计算 |
| `tokens_per_second` | completion_tokens / (llm_ms/1000) | 计算，标注近似 |
| `estimated_cost` | 按配置单价估算 | 环境变量；未配置为 null |
| `citation_ok_rate` | 引用 chunk 在上下文中的回答比例 | chat 后处理 |
| `rerank_degraded_rate` | rerank 降级次数 / 有效 chat 次数 | trace |
| `refusal_rate` | 拒答次数 / chat 次数 | trace |
| `zero_result_rate` | 零命中检索次数 / 检索次数 | trace |
| `queue_wait_p95` | 任务等待时间 p95 | `ingest_jobs` |
| `ingest_duration_p95` | 入库耗时 p95 | `ingest_jobs` |

成本单价只从环境变量读取，不在代码里硬编码厂商价格，也不在 UI 里展示密钥。

## 9. 前端交互

### 9.1 技术形态

- 继续单文件 `app/static/index.html`，原生 HTML/CSS/JS。
- 不引入 CDN、图表库、构建链；图表用内联 SVG。
- 复用现有状态提示、Toast、请求封装；新增 Tab 切换。

### 9.2 轮询与生命周期

| 数据 | 频率 | 说明 |
|---|---|---|
| 驾驶舱摘要 | 3s | Tab 可见时轮询 |
| 任务列表 | 2s | 有 running/queued 时 |
| 运行详情 | 2s | 状态为 queued/running 时 |
| Trace 列表 | 5s | 最近 20 条 |
| 身份/权限 | 切换时 | 租户/用户/KB 变化时 |

- `document.visibilityState === "hidden"` 时暂停轮询。
- 切换 tenant/kb/user 时 `AbortController.abort()`，清空旧数据，避免串租户显示。
- 所有请求错误显示可读横幅，不显示堆栈和密钥。

### 9.3 可视化组件

- **任务看板**：四列状态卡；卡片显示 job_id、doc_id、source、attempts、progress、等待/耗时。
- **任务时间线**：横向阶段条；每个事件显示 stage、status、progress、时间；失败标红。
- **队列 sparkline**：客户端保留最近 30 个采样点，画 queued/running 折线。
- **Trace 瀑布**：每个 stage 一行，条形长度按 `ms` 比例；标注 hits/refused/mode；点击可看详情。
- **评测指标卡**：6 个指标 + 与上次运行的差值箭头；无历史时显示“首次运行”。
- **压测柱状图**：search/chat 的 p50/p95/p99 分组柱；错误率、降级率单独卡片。
- **身份卡**：tenant/user/role/tags/is_admin/可见 KB；三层隔离用状态点。
- **身份对比表**：三列用户，显示 role/tags/hits/top_score/可见 chunk；差异高亮。
- **拒绝原因**：按层展示 allow/deny 和原因，RLS 显示 `bare_count=0` 证据。
- **审计时间线**：actor/action/target/result/reason/time；拒绝用警示色。

### 9.4 只读与开关提示

- `ops.actions_enabled=false` 时，所有触发按钮禁用，显示“只读模式：OPS_ENABLE_ACTIONS=0”。
- 非管理员显示“当前身份无调测操作权限”，但仍可看自己的身份卡和拒绝原因。
- 触发压测/演练前弹出确认，显示固定参数和预计资源占用。

## 10. 错误处理

| 场景 | 行为 |
|---|---|
| 非管理员触发 | 403 + 审计 `denied`，UI 提示原因 |
| 开关关闭 | 403 + 审计 `denied(reason=switch_off)` |
| 同类 run 已在运行 | 409 + 返回已有 run_id |
| 任务不是 failed 却重试 | 409 |
| KB 不属于租户 | 404 |
| parser 探测失败 | `health.parser="down"`，不阻断其他面板 |
| worker 心跳过期 | `stale`，UI 黄色警示 |
| 运行超时 | `failed` + `error=timeout`，输出保留尾部 |
| Trace/评测无数据 | 空状态，不显示假数据 |
| 指标分母为 0 | 返回 `null`，UI 显示“暂无样本” |

## 11. 实现边界与依赖

### 11.1 不修改的 dirty/untracked 文件

以下文件属于他人工作或未提交改动，本期**不修改、不暂存、不提交**：

```text
.env.example
deploy/docker-compose.yml
eval/baseline.json
eval/refusal_calibration.json
eval/baseline_gen.json
eval/baseline_real_gen.json
eval/fixtures/
scripts/calibrate_refusal.py
scripts/run_eval.py
scripts/make_pdf_fixtures.py
src/__init__.py
src/pipeline.py
src/retrieve.py
src/parsers/__init__.py
src/refusal.py
tests/test_api_search.py
tests/test_pipeline.py
tests/test_eval_generate.py
tests/test_pdf_parser.py
```

需要 usage/timing 时，用 `app/routers/chat.py`/`app/services.py` 的包装器，不改 `src/pipeline.py`。需要写评测结果时，只写 `data/ops/`，不覆盖 `eval/baseline*.json`。

### 11.2 允许新增/修改的模块

- 迁移：`migrations/versions/0010_ops_cockpit.py`
- 模型：`src/db/models.py`（新增表）
- 新模块：`src/ops_runs.py`、`src/ops_audit.py`、`src/ingest_events.py`、`src/ops_metrics.py`
- 任务：`src/ingest_jobs.py`（claim/retry/events 挂点）、`src/ingest.py`（阶段事件）、`src/worker.py`（心跳）
- 路由：`app/routers/ops.py`、`app/routers/identity.py`、`app/main.py`（注册）
- 前端：`app/static/index.html`
- 测试：新增 `tests/test_ops_*.py`、`tests/test_ingest_events.py`；必要时只追加不改他人测试
- 文档：`docs/ops-cockpit-checklist.md`、README/ADR 的必要补充

## 12. 测试策略

| 层 | 测试 |
|---|---|
| 迁移 | 0010 升级/降级；新表字段/索引/约束 |
| `ops_runs` | 入队、领取、重复同类拒绝、超时、状态流转、租户隔离 |
| `ops_audit` | allowed/denied/succeeded/failed 记录；跨租户不可见 |
| `ingest_job_events` | 正常阶段顺序；失败事件保留；事件写失败不影响入库 |
| 身份 | 用户不存在降级 guest；role/tags/is_admin 正确；KB 列表租户隔离 |
| 权限解释 | 租户不匹配 404；ACL 空；RLS bare_count=0；reason 可读 |
| 身份对比 | 最多 3 用户；top_k 边界；结果差异；非管理员 403 |
| 摘要 | 队列/Token/拒答/引用/降级率计算；空数据为 null |
| runner | 白名单、shell=False、超时、输出截断、JSON 摘要、失败状态 |
| 重试 | 仅 failed；attempts 重置；事件和审计写入 |
| UI | 现有 `test_api_ui.py` 增加 Tab/关键端点断言；手工验收清单 |
| 回归 | 全量 pytest；Gate C 6/6；demo_ingest；固定参数 loadtest |

手工验收至少覆盖：

1. 驾驶舱能看到真实队列和任务时间线。
2. 切换 guest/member/admin，身份卡和可见 KB 正确。
3. 同一 query 三种身份对比，差异可见。
4. 拒绝原因能指出租户/ACL/RLS 哪一层。
5. 触发 mock 评测，状态 queued→running→succeeded，指标卡出现。
6. 触发固定压测，p50/p95/p99 和错误率出现。
7. 非管理员/开关关闭时按钮禁用，审计有 denied 记录。
8. Trace 列表点开能看到阶段瀑布。

## 13. 分阶段交付

### P1：只读驾驶舱（先看到真实数据）

1. 迁移 0010 + 新模型。
2. job events + worker 心跳。
3. `/v1/identity`、`/v1/ops/permissions`。
4. `/v1/ops/summary`、`/v1/ops/jobs`、`/v1/ops/jobs/{id}/events`、`/v1/ops/traces`。
5. Trace usage/timing 字段与 chat 包装器。
6. UI 三个新 Tab 的只读部分。
7. 单测 + 手工验收。

### P2：受控动作（重试 + 评测 + 审计）

1. `ops_runs`/`ops_audit` 状态机。
2. `scripts/run_ops.py` + 白名单 + 超时。
3. 失败任务重试 + 审计。
4. mock 评测运行 + 历史/对比。
5. UI 按钮、开关、审计时间线。
6. 单测 + API 测试。

### P3：压测、演练与趋势

1. 固定参数 loadtest。
2. 安全故障演练。
3. 最近 20 次运行对比、轻量趋势。
4. 最终验收：全量 pytest、Gate C、demo_ingest、loadtest。

## 14. 风险与应对

| 风险 | 应对 |
|---|---|
| 新表/事件写入影响入库性能 | 事件独立短事务、best-effort、批量/索引控制 |
| ops-runner 执行重活影响 API | 独立进程；唯一运行约束；超时；资源提示 |
| 身份头不是真实鉴权 | UI 明确标注调测占位；生产接 token 后替换 `current_user` |
| RLS 探针误用 | 只返回计数；裸连接只读；不返回跨租户内容 |
| 指标口径被误解 | UI 标注分母/样本/环境；mock 与真实 bge-m3 分开 |
| 覆盖他人 dirty 文件 | 11.1 清单 + 只按文件名暂存；用包装器替代 pipeline 修改 |
| 评测/压测结果污染基线 | 只写 `data/ops/`；不写 `eval/baseline*.json` |
| 前端单文件变大 | 按 Tab/组件函数分区；本期不引入构建链，超过可维护阈值再拆分 |

## 15. 验收标准

1. 四个 Tab 可用，业务工作台原有六项不回归。
2. 驾驶舱展示真实队列、任务时间线、Trace、AI 指标，空数据不伪造。
3. 身份卡展示真实 role/tags/is_admin/可见 KB；三层隔离状态可读。
4. 身份对比能显示 guest/member/admin 的命中差异。
5. 拒绝原因能定位到租户/ACL/RLS 层。
6. 管理员在开关开启时可触发重试、mock 评测、固定压测、安全演练；关闭时全部只读。
7. 所有受控动作有审计记录；拒绝也有记录。
8. 评测/压测结果不覆盖 `eval/baseline*.json`，只写 `data/ops/`。
9. 全量 pytest、Gate C 6/6、demo_ingest、固定参数 loadtest 通过。
10. 手工验收清单与截图归档到 `docs/ops-cockpit-checklist.md`。

## 16. 术语

| 术语 | 解释 |
|---|---|
| Cockpit | 驾驶舱，面向调测/运营的聚合视图 |
| Ops run | 一次受控调测运行（评测/压测/演练） |
| Audit | 审计，记录谁在什么时候做了什么、结果如何 |
| Heartbeat | 心跳，worker 定期写入存活时间 |
| Waterfall | 瀑布图，按阶段展示耗时 |
| P95 | 第 95 百分位延迟 |
| ACL | 文档标签级访问控制 |
| RLS | Postgres 行级安全 |
| Artifact | 运行产物文件，如评测/压测 JSON |
