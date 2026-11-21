# 调测驾驶舱验收清单

## 环境

- 验收日期：2026-11-20
- commit：`d01c656`（Task 15 实现与文档）、`e947718`（ADR 索引）、`6b3b6cf`（浏览器验收证据）
- 运行模式：mock（`EMBED_MODE=mock`；入库与检索使用确定性哈希向量，界面分数不代表真实检索质量）
- `OPS_ENABLE_ACTIONS`：浏览器验收时以 `1` 启动本地 API（`127.0.0.1:8010`），同时运行 `scripts/run_ops.py --interval 2`
- tenant / kb / user：`demo` / `kb_after_sale` / `u_admin`；非管理员场景切到 `u_member`
- 数据库：`127.0.0.1:5433/kb`
- 验证范围：静态页面标记、全量回归、Gate C、P1 入库冒烟、固定压测、浏览器端到端（触发评测、身份对比、演练、非管理员禁用态）

## 功能

> **历史归档（Task 16 之前）**：以下 5 个入口 / 三栏微信桌面版 shell 记录的是 WorkBuddy 改造前的旧 UI，相关测试名（如 `test_ui_has_wechat_desktop_shell`、`test_ui_shell_navigation_markers`、`test_ui_has_cockpit_panels`）已随旧页面删除而不存在。该界面已被 WorkBuddy 七模块入口取代，本段仅保留作为历史证据，不再代表当前实现。

- [x] 三栏微信桌面版 shell，左导航 5 个入口与选中态容器存在（历史）
  - 证据：`tests/test_api_ui.py::test_ui_has_wechat_desktop_shell`、`test_ui_shell_navigation_markers`
- [x] 驾驶舱只读面板齐全：健康、队列、任务、任务事件、AI 指标、Trace 列表与瀑布
  - 证据：`tests/test_api_ui.py::test_ui_has_cockpit_panels`
- [x] 身份卡显示 role / tags / 可见 KB / 三层隔离 / 拒绝原因 / 身份对比面板
  - 证据：`tests/test_api_ui.py::test_ui_has_identity_panels`、`tests/test_identity_compare.py`；浏览器实测见下方端到端记录与 `docs/images/ops-identity-compare.jpg`
- [x] 运行历史、运行详情、最近一次评测指标卡与相对上一轮 delta 容器存在
  - 证据：`tests/test_api_ui.py::test_ui_has_run_metrics_and_delta`
- [x] 受控触发区分三种入口：mock 评测直接提交；固定压测二次确认；安全演练只展开三选一子按钮
  - 证据：`app/static/index.html` 中 `run-trigger-eval` / `run-trigger-loadtest` / `run-trigger-drill` 显式绑定，`data-drill="drill_parser|drill_llm|drill_queue"` 独立确认后触发
- [x] 非管理员或 `OPS_ENABLE_ACTIONS` 关闭时，触发按钮、演练按钮与身份对比按钮同步禁用
  - 证据：`updateActionAvailability()` 读取 `identity.ops.can_trigger`，同时管理三枚触发按钮、`[data-drill]`、`identity-compare-button` 与 `updateRetryButton(...)`；浏览器 u_member 实测三按钮 disabled，见 `docs/images/ops-member-disabled.jpg`
- [x] 浏览器中管理员触发 mock 评测，观察 queued → running → succeeded 并看到指标
  - 证据：run #383 由 `queued` 进入 `succeeded`，Recall@k=0.9342、MRR=0.8575、Hit Rate=0.9474、用例 50；截图 `docs/images/ops-runs.jpg`
- [x] 固定压测出现 p50 / p95 / p99，三项安全演练可运行并产生审计
  - 证据：压测分位见下表（脚本直跑）；演练 run #384/#385/#386 均 succeeded 且 `summary.passed=true`
- [ ] 900px 以下单列布局与键盘遍历导航/按钮

## 证据

### 自动化命令（2026-11-20 本机实际输出）

```text
> py -3.13 -m pytest -q
467 passed, 1 warning in 39.33s

> py -3.13 scripts/demo_acceptance.py
混合检索命中 PASS；跨租户隔离 PASS；RLS 兜底 PASS；ACL 拒绝越权 PASS；
ACL 放行本部门 PASS；版本发布与回滚 PASS；6/6 通过（127.0.0.1:5433/kb）

> py -3.13 scripts/demo_ingest.py --mock
job_id=9858 doc_id=kb_smoke-e72f64ed duplicated=True；status=succeeded progress=100；
检索命中 4 条；PASS upload→ingest→search

> py -3.13 scripts/loadtest.py --concurrency 20 --requests 200 --out data/ops/loadtest/manual.json
每端点 200 请求，并发 20；search 错误率=0.00%，chat 错误率=0.00%，rerank 降级率=0.00%
```

压测报告 `data/ops/loadtest/manual.json`（本地产物，不提交）：

| 端点 | 成功/总数 | 错误率 | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|---:|---:|
| search | 200/200 | 0.00% | 169.30 | 216.60 | 251.68 |
| chat | 200/200 | 0.00% | 731.57 | 1242.61 | 1373.29 |

`timestamp=2026-11-20T10:02:14Z`，`elapsed_ms=10605.35`，`total_requests=400`，参数 `kb_id=kb_after_sale`、`tenant_id=demo`、`mode=hybrid`、`top_k=5`、`audience=public`。

### 浏览器端到端（2026-11-20 18:08–18:13，本地 API 8010 + ops-runner）

- 管理员 `u_admin` 触发 mock 评测：`POST /v1/ops/kbs/kb_after_sale/runs` 返回 `run_id=383`，`queued → succeeded`；指标 Recall@k=0.9342、MRR=0.8575、Hit Rate=0.9474、用例 50；审计记录 `run_eval_mock / allowed / target=383`。
- 三项演练：`drill_parser`(#384)、`drill_llm`(#385)、`drill_queue`(#386) 全部 `succeeded`，`summary.passed=true`。
- 身份对比（query=退款）：guest、u_member、u_admin 各 2 条命中，耗时 250.98 / 188.46 / 136.53 ms。
- 非管理员 `u_member`：三个触发按钮均 disabled（页面标注“只读”）；操作审计按设计返回 403（管理员可见 1 条 allowed 记录）。
- 截图：`docs/images/ops-runs.jpg`、`docs/images/ops-identity-compare.jpg`、`docs/images/ops-member-disabled.jpg`。

### 指标口径

- `recall_at_k`：可回答用例中，前 k 条命中的标注 chunk 比例逐题计算后取均值，多标注可部分得分。
- `mrr`：可回答用例中，前 k 条里首个正确结果的倒数排名均值。
- `hit_rate`：可回答用例中 `recall_at_k` 大于 0 的比例。
- `refusal_accuracy`：不可回答用例中正确拒答的比例。
- `answer_coverage`：可回答用例中未拒答的比例；生成失败单独计入 `counts.generation_errors`。
- `citation_accuracy`：可回答、生成成功且未拒答的回答中，引用 chunk_id 存在于实际上下文的比例均值；无引用回答按 0 计入分母。

### 证据边界

- 浏览器验收在本地 `uvicorn`（8010，`OPS_ENABLE_ACTIONS=1`）与 `scripts/run_ops.py` 下完成；`127.0.0.1:8000` 的旧容器只作对照，不参与验收。
- 未验证项：900px 以下单列布局、键盘遍历导航/按钮；固定压测的 p50/p95/p99 来自 `scripts/loadtest.py` 直跑，未在界面内点击触发后截图。
- `demo` 租户原库没有用户行，验收前补种了 `u_admin`（admin）与 `u_member`（member）两条 `users` 记录；因 `users` 主键是全局 `user_id`，保留会与测试 fixture 冲突，验收后已删除，未随仓库提交。复跑手工验收时需先按同样方式临时补种。
- 压测与评测数字来自本机 mock 模式、已有语料与固定 query，只代表该次环境，不是容量承诺。

---

## Task 16：WorkBuddy 唯一入口与实时验收

### 环境与真实性边界

- 基线：`fb1e0a1c6e2f015c7921cf3a2f02b79708a73e6b`。证据时间（UTC）：原 20 项 10:33:44–10:37:21；补充 2 项 12:31:58–12:33:16，追加写入同一 `final-report.json`；最终日志为 `D:\tmp\task16-live-evidence\final.log`。
- API：真实 Uvicorn/FastAPI，`OPS_ENABLE_ACTIONS=1`，`http://127.0.0.1:58999`；进程 cwd 故意设为 `D:\tmp\task16-live-cwd`，不是仓库目录，用于验证静态资源路径不依赖 cwd。
- 数据库：独立 PostgreSQL/pgvector 数据库 `task16_accept_1795257224`，真实执行 Alembic 0001–0011。一次性容器 `task16-pg-accept-2` 使用 `docker.m.daocloud.io/pgvector/pgvector:pg16`，主机端口 50074；证据收集后已 stop/rm，原有 `rag-kb-pg` 未触碰且保持 healthy。
- 一次性容器为缩短本地验收时间使用 `--no-sync`、`fsync=off`、`synchronous_commit=off`、`full_page_writes=off`，这些设置仅用于可丢弃环境，不是生产持久性配置或验证结论。
- 入库执行真实 `src.worker.worker_once`；受控运行由真实 `scripts/run_ops.py::run_once` 领取和执行。浏览器为本机系统 Chrome，经 Playwright Chromium 协议驱动，不是静态 route mock。
- tenant / KB / admin：`t16_accept` / `kb16_accept` / `u16_admin`；另使用空 KB、member、guest、tagged 和跨租户身份验证隔离。
- 仅外部 OpenAI 兼容 LLM 由本机 `ThreadingHTTPServer` 确定性桩替代；embedding 使用项目自带 `EMBED_MODE=mock`。FastAPI、静态文件、PostgreSQL、迁移、上传、worker、ops-runner 和 Chromium 均运行当前工作区真实实现。

### TDD 与自动化证据

- [x] 旧 wx 页面仍在时先写 WorkBuddy 入口测试并观察真实 RED
  - `py -3.13 -m pytest -q tests/test_api_ui.py tests/test_workbench_ui.py`：`6 failed in 18.08s`。失败直接显示旧 HTML 缺少 `RAG Operations Console`、`/ui/js/app.mjs` 和 WorkBuddy 元素，不是测试语法或环境错误；原文：`D:\tmp\task16-red.txt`。
- [x] Chromium `/v` 语义下的 Trace ID pattern 单独完成 RED/GREEN
  - RED：`py -3.13 -m pytest -q tests/test_workbench_ui.py::test_trace_id_pattern_is_valid_in_chromium`，`1 failed in 1.74s`；非法 `trace/invalid` 被 Chromium 当作有效值，见 `D:\tmp\task16-pattern-red.txt`。
  - 最小修复：仅将 `#trace-id` 改为 `pattern="[A-Za-z0-9][A-Za-z0-9_.:\-]{0,159}"`，保持允许字符不变。
  - GREEN：同一测试 `1 passed in 4.86s`，见 `D:\tmp\task16-pattern-green.txt`。
- [x] 目标 UI Python 自动化：`45 passed in 57.13s`（2026-11-21 复核：`py -3.13 -m pytest -q tests/test_api_ui.py tests/test_workbench_ui.py`）；Task 16 原始证据见 `D:\tmp\task16-ui-final.txt`。
- [x] JavaScript：Node TAP 执行 `tests/js` 下 core、overview、lab、traces 四个测试文件，`# tests 107`、`# pass 107`、`# fail 0`（2026-11-21 复核：`node --test tests/js/*.test.mjs`）；Task 16 原始证据见 `D:\tmp\task16-node-final.txt`。
- [x] 首次完整 Python 自动化：`py -3.13 -m pytest -q`，`661 passed, 1 skipped, 1 warning in 647.85s`，见 `D:\tmp\task16-pytest-all-final.txt`。唯一跳过为开发数据库当时不可用导致的 readiness 用例；该用例随后以 `py -3.13 -m pytest -q tests/test_api_health.py::test_ready_ok_when_db_up` 独立复跑，`1 passed in 10.64s`，见 `D:\tmp\task16-ready-skip.txt`。独立复跑不并入首次全量统计。
- [x] 开发数据库可用后的完整 Python 清洁复跑（2026-11-21 复核）：`py -3.13 -m pytest -q`，`663 passed, 1 warning in 111.60s`，无跳过、无失败，见 `D:\tmp\r10-pytest-all-clean.txt`；唯一 warning 为既有 Alembic `path_separator` DeprecationWarning。复核时临时暂停了会抢占测试队列的 `rag-kb-worker` 容器，结束后已恢复。

### 实时 FastAPI / Chromium 验收矩阵

原 20 项实时矩阵：`20 passed / 0 failed / 0 running`。第 2 次规格审查后补充 2 项：`2 passed`；合并后 `22 passed / 0 failed / 0 running`。每项均由 `final-report.json` 的同名 check 记录动作与观察值。

- [x] 01 `live_fastapi_static_arbitrary_cwd`：任意 cwd 下 `/live`、`/ready`、`/ui/`、CSS、`app.mjs`、`overview.mjs` 均 200；`/ui` 为 307 且 Location 指向 `/ui/`；已删除的 `/ui/workbench.html` 为 404。
- [x] 02 `live_upload_worker_and_acl_seed`：真实 API 上传 job #1，真实 worker 后 `succeeded / progress=100 / attempts=1`，生成 4 chunks；内部章节设置 finance ACL，供后续身份比较。
- [x] 03 `live_search_chat_refusal_error_cancelled_sse`：Search 200；Chat 依次收到 `hits`、`delta`、`[DONE]`；拒答收到 `[DONE]`，LLM 故障返回 `event:error` JSON message；原始 socket 断开落库为 `cancelled`。指标各有一条 chat `succeeded/refused/error/cancelled` 和一条 search `succeeded`。
- [x] 04 `live_trace_cursor_contract`：缺少成对 cursor 参数返回 422；第一页 trace id=5、第二页 id=4，两页不重复且第二页继续使用返回的 opaque cursor。
- [x] 05 `search_chat_success_during_metric_outage`：数据库触发器同时拒绝 HTTP/RAG 指标写入时，Search 仍 200，Chat 仍完成 hits/delta/`[DONE]`；对应两个 trace 的指标行均为 0，证明指标故障没有改成业务失败。
- [x] 06 `chromium_static_entry`：真实 `/ui/` 标题为 `RAG Operations Console`，七个模块导航齐全，CSS/ES modules 实际加载，最终 URL 为 `/ui/#/overview`。
- [x] 07 `overview_four_windows_empty_low_sample_unconfigured`：空 KB 在 1h/24h/7d/30d 均显示 Requests=0、三个空趋势及低样本提示，成本为 Not configured；有数据 KB 显示 Requests=6 和低样本提示。
- [x] 08 `responsive_1440_1280_900_390`：四种 viewport 的 `scrollWidth` 分别严格等于 1440/1280/900/390，无横向溢出；布局依次为 wide/drawer/drawer/mobile，1280/900 详情为 `dialog`，390 为单列 `tabpanel`。
- [x] 09 `keyboard_only_navigation_and_tabs`：只用 Tab/Enter 从品牌、Overview 到 Query Lab 并进入 `#/lab`；只用 ArrowRight/Home/End 操作内部 tabs，顺序和选中态符合预期。
- [x] 10 `live_browser_search_and_trace_link`：Query Lab 实际搜索得到 2 hits，状态为 hybrid；`X-Trace-Id=7393550d44cb`，页面深链指向 `#/traces/7393550d44cb`。
- [x] 11 `live_browser_chat_sse_and_citation`：浏览器实际消费 SSE，完成时显示 2 citations；回答包含“退款将在 3 个工作日内到账”和真实 chunk 引用，trace 为 `ea743b0f2ea3`，usage/timings 与服务端指标同时展示。
- [x] 12 `live_browser_upload_ingestion_poll`：Chromium 选择 Markdown 上传，真实 worker 异步入库，UI 轮询 job #2 到 `succeeded / 100%`。
- [x] 13 `trace_cursor_filters_deep_link_raw_copy`：limit=1 后点击 Load more 得到 2 行；operation/outcome/mode/min_ms/exact trace 组合过滤、深链和键盘 tabs 均正确。Raw 长度 1461；Windows 剪贴板把 63 个 LF 转为 CRLF，规范化后文本逐字相等，且 `json.loads` 后 JSON 完全相等。
- [x] 14 `trace_drawer_and_mobile_interaction`：1280 drawer 有 `aria-modal`、初始关闭按钮焦点、Shift+Tab 焦点圈、Escape 关闭及触发点焦点恢复；390 选择 trace 后隐藏列表，List/Detail 的 ArrowRight 切换可用。
- [x] 15 `overview_pricing_partial_failure_stale`：完整和部分定价分别显示 Complete estimate / Partial estimate；真实 summary 500 时页面保留同 scope 缓存，显示 `stale-at` 与 Last success，trends 仍独立更新。
- [x] 16 `non_admin_cross_tenant_scope_invalidation`：member 的五个动作按钮全部 disabled，直接 API 返回 403；跨租户 API 返回 404；慢 SSE 期间切换 KB 后，迟到回答为空且旧 trace 隐藏。
- [x] 17 `metric_write_failure_and_missing_evidence`：RAG 指标写故障时真实 Search 仍 200；浏览器直开 trace `9e69be04584a` 显示 Missing metric 警告；`task16metricnoevents` 的详情明确返回 `missing: ["events"]`。该项当时只用 API 校验 missing events；Chromium 观察已在补充验收 `chromium_missing_events_observation` 中补齐（见下节）。
- [x] 18 `ingestion_failure_ui_retry_success`：暂时移走已上传文件，job #3 经真实 worker 三次尝试后 terminal failed，错误为 `FileNotFoundError`；Chromium 点击 Retry 被接受，恢复文件后真实 worker 成功，UI 为 `succeeded / 100%`。
- [x] 19 `browser_trigger_eval_loadtest_three_drills`：浏览器逐一点击评测、固定压测和三类演练，真实 `run_once` 执行 run #1–#5；全部 `succeeded / exit_code=0`，UI 回读终态，评测与压测 artifact 已保存并另行读取核对。
- [x] 20 `identity_comparison_and_audit`：guest/tagged/admin 命中数为 3/4/4；Audit 实际七行包括 member `run_eval_mock/denied`、admin `job_retry/allowed`、allowed eval/loadtest 和三项 drill，不是只做页面子串匹配。

### 补充验收：纯键盘链路与 Trace missing events（第 2 次规格审查）

第 2 次规格审查指出：原 20 项中的键盘证据只覆盖品牌/导航/内部 Tab，scope、提交、Trace 选择、抽屉、复制、五类触发和 Retry 实际由 `fill()` / `.click()` 完成；missing events 也只有 API 校验。为此补做以下真实验收，原 20 项结论保持不变。

- 环境：复用现有 `rag-kb-pg`（`127.0.0.1:5433`，Alembic 0011 head），在同一实例创建一次性数据库 `task16_supplement_1795264318`，使用唯一 tenant/KB `t16_supplement_1795264318` / `kb16_supplement_1795264318` 与 `u16_admin`。真实 Uvicorn/FastAPI（8000，`OPS_ENABLE_ACTIONS=1`）、真实上传、`src.worker.worker_once`、`scripts.run_ops.run_once`、系统 Chrome（Playwright Chromium 协议）；外部 LLM 仍为本机确定性桩，embedding 仍为 `EMBED_MODE=mock`。开发库、开发 worker 与 `rag-kb-pg` 的配置均未改动，补验收数据只写入一次性数据库。
- 新增 check `keyboard_only_full_acceptance_chain`：**passed**，173 步；按 `observation.steps` 统计：Tab 78、Shift+Tab 64、Enter 20、ArrowRight 3、ArrowDown 1、Escape 2、键盘输入 5；`check.duration_ms=60195.9`（约 60.2s，取自 `final-report.json`）。所有交互只使用 Tab/Shift+Tab/Enter/ArrowRight/ArrowDown/Escape 和键盘输入，脚本不使用 `fill()` 或 `.click()`。每一步的实际按键、焦点元素、`location.hash` 和应用状态都写入 `final-report.json` 的 `observation.steps`。
- 实际链路与观察：
  - scope：从文档起始 Tab 到 `#scope-tenant`，键盘输入 tenant、KB、user；在 `#scope-window` 按 ArrowDown 从 24h 切到 7d；Tab 到 `#scope-refresh` 按 Enter。overview summary 与 trends 均为 `success`，scope 为 `t16_supplement_1795264318 / kb16_supplement_1795264318 / u16_admin / 7d`。
  - nav：Shift+Tab 到 `[data-nav="lab"]`，Enter 后进入 `#/lab`。
  - 检索：Tab 到 `#search-query` 输入“退款多久到账”，Tab 到 `#search-button` 按 Enter；状态 `Search complete · 2 hits · hybrid`，trace `c4f2082a7fdf`。
  - SSE：Shift+Tab 到 `[data-lab-tab="search"]`，ArrowRight 选中 Chat（`aria-selected=true`）；Tab 到 `#chat-question` 输入问题，Tab 到 `#chat-button` 按 Enter；状态 `Answer complete · [DONE] received · 2 citations`，回答“退款将在 3 个工作日内到账”，trace `b620c0f4b9cd`。
  - Trace 选择与抽屉：Shift+Tab 到 Traces nav，Enter；Tab 到 `#trace-apply` 按 Enter 加载列表；Tab 到第一行 `[data-trace-select]`，Enter 打开抽屉；`role=dialog`、`aria-modal=true`、初始焦点 `trace-drawer-close`；Shift+Tab 后焦点仍在 `#trace-detail` 内；Escape 关闭并把焦点还给所选 trace 行（`b620c0f4b9cd`）。
  - Raw 复制：再次 Enter 重开抽屉，Tab 到 Summary，ArrowRight→Timing→Raw（`aria-selected=true`）；Tab 到 `#trace-copy` 按 Enter，状态包含 Copied；Raw 1816 字节，剪贴板 `json.loads` 与 Raw 完全相等。
  - 五类触发：Shift+Tab 到 Evaluation nav，Enter；Tab 到 `#evaluation-load` 按 Enter；逐个 Tab 到五个触发按钮并 Enter。真实 `run_once` 执行 run #1–#5，全部 `succeeded / exit_code=0`，UI 分别显示 “Run #N reached terminal status: succeeded.”。
  - Retry：Shift+Tab 到 Ingestion nav，Enter；Tab 到 `#ingestion-load` 按 Enter；Tab 到失败 job #2 行（`data-job-select=2`）按 Enter；Tab 到 `#ingestion-retry` 按 Enter，状态 “Retry accepted for job #2; reloading actual state…”。真实 worker 后 job #2 `succeeded / attempts=1 / progress=100`，失败原因仍为真实 `FileNotFoundError`（上传文件在首次 worker 前被移走）。
  - 观察到的限制：Retry 激活后视图重渲染，焦点回到 `body`（步骤记录如实保留）。按钮本身键盘可达且动作成功，不属于不可达缺陷，但这是一处焦点保持缺口。
- 新增 check `chromium_missing_events_observation`：**passed**。在一次性库插入一条只有 metric、无 JSONL events 的 trace `task16metricnoevents`；Chromium 打开 `http://127.0.0.1:8000/ui/#/traces/task16metricnoevents`（1440×1000）。`#trace-detail-status` 为 “Trace task16metricnoevents loaded with missing evidence.”，`#trace-missing` 明确显示 “Missing events: no scoped JSONL events were recorded; Raw contains only available evidence.”；API 同时返回 `missing: ["events"]`、`events: []`。截图：`D:\tmp\task16-live-evidence\traces-missing-events.png`。
- 合并结果：原 20 项 + 补充 2 项 = `22 passed / 0 failed / 0 running`；`final-report.json` 保留原 20 项结论并追加两个新 check，未把任何未执行项标为 passed。
- 控制台：补充运行只有两次 `/favicon.ico` 404，console location 已记录为 `http://127.0.0.1:8000/favicon.ico`；无 API、静态模块或脚本错误，`http_errors` 为空。
- 失败与恢复：补验收期间 Docker Desktop 曾多次重启，导致数据库进入 recovery。`task16-supplement-3.log` 为 0 字节，该次运行启动即受阻、无输出；失败依据为 `task16-supplement-4.log` 中 psycopg `OperationalError: ... FATAL: the database system is in recovery mode` 的 traceback；最终取证运行为 `task16-supplement-1.log`（2894 字节），在引擎稳定窗口内完整通过，失败尝试未计入 22 项。

### 受控运行实际数值

| 运行 | 实际观察 |
|---|---|
| eval_mock #1 | 50 cases（38 answerable / 12 unanswerable）；Recall@k=0.9342、MRR=0.8575、Hit Rate=0.9474；exit 0。Artifact：`D:\tmp\task16-live-evidence\runs\data\ops\eval\1.json`。 |
| loadtest #2 | 并发 20，每端点 200；Search 200/200、Chat 200/200，失败均 0。Search p50/p95/p99=176.75/1464.78/1467.05 ms；Chat=348.88/758.32/996.85 ms；rerank degradation=0/200。Artifact：`D:\tmp\task16-live-evidence\runs\data\ops\loadtest\2.json`。 |
| drill_parser / drill_llm / drill_queue #3/#4/#5 | 均 `succeeded`、exit 0，`summary.passed=true`；Audit 分别留下 allowed 记录。 |

这些数值来自本机隔离环境、确定性 LLM 桩、mock embedding、固定语料和固定 query，只描述本次运行，不是外部模型质量结论或生产容量承诺。

### 故障注入、控制台与证据

- 指标故障通过在隔离库临时创建触发器真实拒绝 INSERT 注入；summary 故障通过临时改名 `ops_runs` 注入，观察后立即恢复；入库故障通过暂时移动上传文件注入，重试前恢复。没有修改业务代码来伪造故障或成功。
- cancelled 使用真实原始 socket 断开；scope 失效使用慢 LLM 响应；这三类异常和重试堆栈均保留在 `final.log`，并与数据库/UI 终态交叉核对。
- 最终控制台只有三类资源消息：一个 404 和两次 intentional summary 500。独立诊断 `D:\tmp\task16-console-diagnostic.json` 证明 404 URL 为 `/favicon.ico`；`http_errors` 仅含两条 `/v1/ops/summary?...window=24h` 500。没有最终 invalid-pattern、ES module 或其他静态资源错误。
- 13 张 run 7 截图位于 `D:\tmp\task16-live-evidence\`：空/低样本 Overview、stale/partial、1440/1280/900/390 响应式、Search/Chat、Raw、移动 List、入库重试、五类 runs、Audit；已逐张检查界面状态与报告观察值一致。补充验收新增 `traces-missing-events.png`，同样已目视核对。
- 中间失败报告和日志以 `fourth-run-*`、`fifth-run-*`、`sixth-run-*` 保留，未将早期 17/20 或 18/20 当作最终验收结果；最终结论只取 run 7 与补充验收的 `final-report.json` / `final.log`。
- 临时 harness、日志、截图和 artifact 均在 `D:\tmp`；未覆盖仓库 eval baseline，也未把静态 route mock 当作实时 API 验收。

### 未覆盖边界

- 原 20 项与补充 2 项均已实际执行。第 2 次规格审查指出原“仅键盘”表述过宽，已由 `keyboard_only_full_acceptance_chain` 用真实按键链补齐；missing events 已由 Chromium 观察补齐。以下是仍未覆盖的环境边界，不伪装成生产验证。
- 未调用真实外部 LLM，未验证真实 embedding 的检索质量、费用和延迟；确定性桩与 mock 分数不能代表外部服务质量。
- 浏览器仅覆盖本机 Chromium；未覆盖 Firefox、WebKit/Safari、物理移动设备、屏幕阅读器实机。
- 未覆盖生产多实例并发、容器编排、TLS、备份恢复和持久性；run 7 的一次性 PostgreSQL 非生产 fsync 设置尤其不能用于证明耐久性。补充验收复用现有 `rag-kb-pg`，未使用该一次性配置，但其结果同样只代表本机开发环境。
- 未配置的外部解析服务集成未覆盖；parser drill 仅证明本次环境下真实 runner 与本地解析路径。
- 固定压测只覆盖单进程、本机、固定 query 的短时段运行，不构成生产容量或 SLA 结论。
