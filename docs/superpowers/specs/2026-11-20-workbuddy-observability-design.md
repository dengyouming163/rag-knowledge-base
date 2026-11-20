# WorkBuddy 专业工作台与完整可观测性设计

- 日期：2026-11-20
- 状态：已确认，待书面评审
- 仓库：`projects/rag-knowledge-base`
- 入口：`/ui`

## 1. 目标

将现有微信三栏式调测页面重构为白色为主、面向研发/算法/运维人员的专业工作台：

1. 采用已确认的方案 A：WorkBuddy 式白色应用壳、左侧导航、右侧单页工作区。
2. 在 Traces 中采用方案 C 的 Trace Inspector，提供列表、真实阶段跨度与原始证据。
3. 保留并重构上传、入库、检索、SSE 问答、任务重试、评测/压测/演练、身份对比和审计能力。
4. 新增 Postgres 请求指标，支持 QPM、错误率、分位延迟、趋势、Token、成本估算和专业筛选。
5. 明确所有指标口径、样本量、统计窗口、空值与异常状态，不用 `0` 冒充缺失数据。

## 2. 已确认决策

- 布局：方案 A，并加入方案 C 的 Trace Inspector。
- 范围：完整可观测性升级，而非仅改前端样式。
- 存储：新增 Postgres 指标表，不采用 JSONL 增量聚合或外部时序系统。
- 功能：现有上传、入库、检索测试、问答调试与驾驶舱全部保留并重构。
- 视觉：白色为主，蓝紫色仅用于选中态与关键操作。
- 页面：一个 `/ui` 单页入口，模块与内部 Tab 切换时不做整页跳转。

## 3. 非目标

- 不引入 Prometheus、Grafana、ClickHouse 或其他外部可观测性服务。
- 本版不建设分钟预聚合、降采样或自动 retention 任务；所有查询限制在有界时间窗内。
- 不改变检索、拒答、引用、ACL、RLS、SSE 协议和入库发布语义。
- 不把成本估算当作真实账单，不写死任何模型价格。
- 不采集请求头、完整 HTTP body、模型密钥或异常堆栈到指标表。
- 不通过前端禁用按钮替代后端权限校验。

## 4. 信息架构

### 4.1 应用壳

左侧白色导航：

| 模块 | 内容 |
|---|---|
| Overview | 运行总览、趋势、检索质量、Worker 状态 |
| Retrieval Lab | 检索测试、问答调试、上传与入库 |
| Traces | 调用列表、阶段详情、Trace Inspector |
| Ingestion | 任务队列、任务详情、阶段事件、受控重试 |
| Evaluation | 评测、固定压测、安全演练、历史对比 |
| Access | 身份、权限解释、身份切换对比 |
| Audit | 受控操作审计时间线 |

顶部统一承载：Tenant、KB、User、时间窗口和刷新。时间窗口只影响支持时间序列的模块；不适用于当前视图的控件必须禁用并说明原因，不能悄悄忽略。

### 4.2 单页与深链接

- 左侧导航切模块，模块内部用 Tab 切子视图。
- URL hash 保存模块、Tab、选中 Trace 和时间窗口。
- 刷新或打开分享链接后恢复视图；无效 hash 回退到 Overview。
- 切换作用域时清理不再适用的选中项和筛选条件。

### 4.3 前端代码组织

“单页”指单一 `/ui` 入口，不要求继续把所有代码塞入一个 HTML 文件。为控制当前单文件的复杂度，使用浏览器原生 ES modules 拆分 API、状态、格式化、组件和各视图；保留原生 HTML/CSS/JS，不引入前端框架或构建步骤。StaticFiles 继续直接提供服务。

## 5. 视觉与组件规范

- 页面底色为浅灰，主要表面为白色，使用细分隔线和克制的阴影。
- 蓝紫色用于当前导航、Tab、链接和主要按钮；错误、警告、成功使用语义色。
- 数字使用 tabular numbers；Trace ID、参数、JSON 使用等宽字体。
- 所有指标卡包含名称、值、单位、统计窗口和样本量。
- 区分加载、空数据、无权限、未配置、查询失败、低样本和过期缓存。
- 趋势图使用内联 SVG，提供坐标单位、时间范围、 tooltip 和键盘可读摘要，不引入大型图表库。
- 所有动态文本经过转义；原始 JSON 使用文本节点渲染，不进入 `innerHTML`。

## 6. Postgres 数据模型

新增 Alembic 迁移 `0011_ops_observability` 和对应 ORM 模型。表结构只能通过迁移变更。

### 6.1 `ops_http_metrics`

每个带有效 `X-Tenant-Id` 的业务 API 请求记录一行；缺失 Tenant 的请求不进入租户报表。

| 字段 | 类型与约束 | 说明 |
|---|---|---|
| `id` | `bigserial PRIMARY KEY` | 游标次序 |
| `occurred_at` | `timestamptz NOT NULL DEFAULT now()` | 请求完成时间 |
| `tenant_id` | `text NOT NULL` | 只来自 `X-Tenant-Id` |
| `kb_id` | `text NULL` | 能可靠取得时记录；否则为空 |
| `route` | `text NOT NULL` | 路由模板，不存原始高基数 URL |
| `method` | `text NOT NULL` | HTTP 方法 |
| `status_code` | `smallint NOT NULL` | 100–599 |
| `duration_ms` | `double precision NOT NULL CHECK >= 0` | 完整响应耗时 |
| `trace_id` | `text NULL` | 关联 RAG 请求 |

索引：

- `(tenant_id, occurred_at DESC, id DESC)`
- `(tenant_id, kb_id, occurred_at DESC)`

不设置外键：即使租户/KB 不存在或请求最终为 404，也应能记录事实，且指标写入不能反过来影响业务请求。所有读取仍必须显式按当前 Tenant 过滤。

### 6.2 `ops_rag_metrics`

每次 Search / Chat 在终态记录一行，不在每个阶段重复写数据库。

| 字段 | 类型与约束 | 说明 |
|---|---|---|
| `id` | `bigserial PRIMARY KEY` | 游标次序 |
| `trace_id` | `text NOT NULL` | 单次调用关联键 |
| `tenant_id` | `text NOT NULL` | 当前 Tenant |
| `kb_id` | `text NOT NULL` | 当前 KB |
| `occurred_at` | `timestamptz NOT NULL DEFAULT now()` | 终态时间 |
| `operation` | `text NOT NULL` | `search` 或 `chat` |
| `mode` | `text NOT NULL` | 实际检索模式 |
| `outcome` | `text NOT NULL` | `answered/refused/error/cancelled` |
| `hits` | `integer NOT NULL CHECK >= 0` | 最终命中数 |
| `citation_ok` | `boolean NULL` | 未发生生成时为空 |
| `rerank_degraded` | `boolean NULL` | 未执行 rerank 时为空 |
| `total_ms` | `double precision NOT NULL CHECK >= 0` | 请求端到端耗时 |
| `rewrite_ms` | `double precision NULL CHECK >= 0` | 未改写时为空 |
| `retrieval_ms` | `double precision NULL CHECK >= 0` | 检索耗时 |
| `rerank_ms` | `double precision NULL CHECK >= 0` | 未 rerank 时为空 |
| `llm_ms` | `double precision NULL CHECK >= 0` | 未生成时为空 |
| `model` | `text NULL` | 实际生成模型 |
| `prompt_tokens` | `integer NULL CHECK >= 0` | 未生成时为空 |
| `completion_tokens` | `integer NULL CHECK >= 0` | 未生成时为空 |
| `error_kind` | `text NULL` | 异常类别，不存堆栈 |
| `stage_spans` | `jsonb NOT NULL DEFAULT '[]'` | 有序阶段跨度数组 |

约束与索引：

- `UNIQUE (tenant_id, trace_id)`，保证单次请求终态幂等。
- `(tenant_id, kb_id, occurred_at DESC, id DESC)`。
- `stage_spans` 在应用层验证为有限数组，每项只允许白名单字段和有限数值。

`stage_spans` 示例：

```json
[
  {"stage":"rewrite","start_ms":0.0,"duration_ms":18.2,"status":"ok"},
  {"stage":"retrieve","start_ms":18.2,"duration_ms":86.4,"status":"ok"},
  {"stage":"answer","start_ms":104.6,"duration_ms":731.0,"status":"ok"}
]
```

固定延迟列用于 SQL 聚合；`stage_spans` 只用于单次诊断，避免报表依赖 JSONB 展开。

### 6.3 与现有 Trace JSONL 的关系

- 现有 JSONL 继续记录，保持原始阶段证据和兼容性。
- Postgres 是筛选、聚合和趋势的唯一数据源。
- Trace 详情合并 Postgres 指标行与 JSONL 原始事件；任一部分缺失时明确标记，不伪造另一部分。
- 不再用全量 Trace 扫盘计算 Overview 指标。

## 7. 采集设计

### 7.1 HTTP 指标

- 使用 ASGI 中间件，在最终响应完成后写入。
- SSE 必须覆盖整个响应流，而非只测到响应头发送。
- 跳过 `/v1/ops/*`，防止仪表盘轮询污染自身 QPM。
- `route` 取路由模板；无法匹配时使用固定 `unmatched`，不回退到原始路径。
- `kb_id` 只从已校验的路径参数或路由显式设置的 request state 获取，不读取 body 猜测。
- 写库失败只记录日志，原响应不变。

### 7.2 RAG 指标

- Search 与 Chat 在 `answered/refused/error/cancelled` 终态各写一次。
- 通过现有代理/包装器采集，不修改他人尚未提交的 `src/pipeline.py`：扩展 chat client 调用记录以区分 rewrite 与最终生成，扩展 retriever 计时，并为 reranker 增加透明计时代理。
- 未发生的阶段写 `NULL`，不写 `0`。
- Chat 的生成 Token 只取最终答案生成，不把多轮 rewrite 的残留误记为 answer usage。
- 早退拒答没有生成时，`model`、Token、`llm_ms`、`citation_ok` 均为空。
- 异常只记录异常类别；取消必须记录为 `cancelled`。
- 指标写入失败不改变检索结果、答案、SSE 帧或任务状态。

## 8. 指标口径

### 8.1 时间窗与桶

| 窗口 | 桶宽 |
|---|---|
| `1h` | 1 分钟 |
| `24h` | 5 分钟 |
| `7d` | 1 小时 |
| `30d` | 6 小时 |

所有窗口使用 UTC 计算，前端按本地时区展示并注明。窗口边界必须半开 `[start, end)`，避免相邻桶重复计数。

### 8.2 HTTP

- Requests：窗口内请求数。
- Avg QPM：Requests / 窗口分钟数，必须标注为窗口平均值。
- 4xx Rate：4xx / Requests。
- 5xx Rate：5xx / Requests。
- HTTP p50/p95/p99：对 `duration_ms` 在原始终态行上计算，不能平均各桶分位数。

### 8.3 RAG

- Zero-hit Rate：`hits = 0` 的检索操作 / 有检索结果的操作数。
- Refusal Rate：`outcome = refused` / 已完成的 Chat 数；error/cancelled 不进入分母。
- Rerank Degraded Rate：`rerank_degraded = true` / 实际执行 rerank 的操作数。
- Citation OK Rate：`citation_ok = true` / `citation_ok IS NOT NULL` 的生成请求数。
- E2E/Retrieve/Rerank/LLM p50/p95/p99：只对对应字段非空的请求计算，并返回各自样本量。
- Token：分别汇总 Prompt、Completion 和 Total；未生成不计为一次真实生成调用。

### 8.4 成本

通过 `OPS_MODEL_PRICING_JSON` 配置模型单价，单位为 USD / 1M tokens：

```json
{
  "deepseek-chat": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0}
}
```

- 价格必须是有限非负数；非法配置标记为不可用并记录日志，不能导致 API 失败。
- 返回 `estimated_cost_usd`、`priced_tokens`、`unpriced_tokens`、`pricing_complete`。
- 未配置时成本为 `null`；部分模型有价格时必须显示“部分估算”，不能显示为完整成本。
- 示例中的零仅表示配置格式，不是默认价格；未配置绝不按零成本展示。

### 8.5 队列与离线质量

- 保留 Queued、Running、Oldest Queued、Oldest Running、失败数和 Worker 心跳。
- 保留 Recall@K、MRR、HitRate、Refusal Accuracy、Answer Coverage、Citation Accuracy，键名与 `scripts/run_eval.py` 一致。
- 离线指标来自明确选中的评测运行，不能与在线窗口指标混算。

## 9. API 设计

### 9.1 摘要

`GET /v1/ops/summary?window=1h|24h|7d|30d`

保留现有响应字段以兼容已有消费者，新增 `observability`：

- `window/start_at/end_at/generated_at`
- `http`：请求数、QPM、4xx/5xx、p50/p95/p99、样本量
- `rag`：操作数、结果数、Zero-hit、Refusal、Degraded、Citation、各阶段分位数及样本量
- `tokens`：Prompt、Completion、Total、成本估算及价格覆盖状态

摘要只服务轻量卡片，不携带趋势点。

### 9.2 趋势

`GET /v1/ops/metrics/timeseries?window=...&kb_id=...`

- 只在 Overview 可见时请求和刷新。
- 返回桶宽、起止时间及固定长度数组；无数据的桶按语义返回零计数或空分位数。
- 序列包含 requests、4xx/5xx、E2E p95、Retrieve p95、LLM p95、Token 和成本。
- 不通过平均桶分位数生成窗口总分位数。

### 9.3 Traces

`GET /v1/ops/traces` 在保留现有兼容行为的基础上，为专业列表提供：

- `window/outcome/operation/mode/min_ms/trace_id/limit`
- `before_occurred_at + before_id` 游标
- 稳定按 `(occurred_at DESC, id DESC)` 排序

`GET /v1/ops/traces/{trace_id}/detail` 返回：

- Postgres 指标摘要
- 真实 `stage_spans`
- 现有 JSONL 原始事件
- 数据完整性与缺失说明

所有列表与详情严格按 Tenant/KB 隔离；跨租户资源返回 404。

## 10. Trace Inspector 与交互

### 10.1 三栏诊断

- 左栏：筛选与调用列表，每项显示时间、Trace ID、outcome、hits、E2E 和 Token。
- 中栏：请求归属、模型、结果、Citation、Degraded、本次实际耗时与阶段图。
- 右栏 Inspector：`Summary`、`Timing`、`Raw` 三个 Tab，支持复制原始 JSON。
- 瀑布图使用真实 `start_ms + duration_ms`；未计时部分显示为 `other/uncounted`，不能将阶段耗时随意拼成假时间线。

### 10.2 响应式与键盘

- `>=1440px`：列表、详情、Inspector 三栏。
- `900–1439px`：列表 + 详情，Inspector 为右侧抽屉。
- `<900px`：导航折叠，内容单栏，列表与详情通过内部 Tab 切换。
- 键盘可完成导航、筛选、选择 Trace、关闭抽屉、复制 JSON 和触发受控动作。

### 10.3 模块行为

- Overview：时间窗联动卡片和趋势；只在文档可见且模块激活时刷新。
- Retrieval Lab：保留检索与 SSE 问答，展示 usage/timings，并可跳转到对应 Trace。
- Ingestion：任务与阶段事件联动，失败任务显示重试入口及不可用原因。
- Evaluation：触发后定位运行详情，展示 exit code、产物、输出尾部、指标及与上一次完成运行的差值。
- Access/Audit：统一为专业表格、参数说明和时间线。

全局作用域切换时递增 epoch 并取消旧请求；所有响应提交前检查 epoch，防止旧数据覆盖新作用域。自动刷新不得改变用户当前选中的 Trace、滚动位置或正在编辑的表单。

## 11. 权限、安全与异常

- Tenant 只取 `X-Tenant-Id`；body 不接受 `tenant_id`。
- 读取按现有身份规则开放；触发、重试等写操作继续要求管理员和 `OPS_ENABLE_ACTIONS=1`。
- 审计仍采用 best-effort，不能掩盖原始 403/404/409。
- 原始 JSON、查询文本和错误信息全部转义；复制使用明确用户操作。
- 指标未配置、样本为零、字段未发生、接口失败、无权限和缓存过期必须具有不同视觉与文案。
- 局部查询失败只影响对应卡片或面板；使用旧数据时标记最后成功时间。
- 指标采集与查询均不得绕过 Tenant/KB 过滤，也不得暴露其他租户的资源是否存在。

## 12. 验收标准

### 12.1 自动化

- Alembic upgrade/downgrade、ORM 注册、索引、约束和序列权限。
- 固定时间数据下的窗口边界、QPM、4xx/5xx、零命中、拒答、分位数、Token 与成本。
- HTTP 200/4xx/5xx、SSE、取消、缺失 Tenant、路由模板和采集失败。
- Search/Chat/拒答/异常/取消只写一次 RAG 终态，未发生字段为 `NULL`。
- 摘要、趋势、列表游标、Trace 详情的 Tenant/KB 隔离与参数校验。
- 原有关键 API 与 469 项回归测试继续通过，并新增本设计测试。

### 12.2 浏览器

至少覆盖 1440、1280、900、390px 和键盘操作：

- 七个模块、内部 Tab、hash 恢复和作用域切换。
- Overview 卡片、趋势、空数据、未配置价格、局部失败和过期状态。
- Search/Chat/SSE 结果跳 Trace；列表筛选、加载更多、三栏/抽屉/单栏 Inspector。
- 上传、入库、重试、评测/压测/演练、身份对比与审计。
- 非管理员按钮和接口、跨租户 404、旧响应隔离。
- 采集写库失败时，检索、问答和 SSE 仍正常完成。

### 12.3 完成定义

只有同时满足以下条件才算完成：功能与视觉按本设计实现、全量测试通过、关键浏览器路径通过、指标口径可解释、权限与租户隔离验证通过、未修改或提交他人现有 dirty/untracked 文件。
