# 调测驾驶舱验收清单

## 环境

- 验收日期：2026-11-20
- commit：`d01c656`（Task 15 实现与文档）+ `e947718`（ADR 索引）
- 运行模式：mock（`EMBED_MODE=mock`；入库与检索使用确定性哈希向量，界面分数不代表真实检索质量）
- `OPS_ENABLE_ACTIONS`：浏览器验收时以 `1` 启动本地 API（`127.0.0.1:8010`），同时运行 `scripts/run_ops.py --interval 2`
- tenant / kb / user：`demo` / `kb_after_sale` / `u_admin`；非管理员场景切到 `u_member`
- 数据库：`127.0.0.1:5433/kb`
- 验证范围：静态页面标记、全量回归、Gate C、P1 入库冒烟、固定压测、浏览器端到端（触发评测、身份对比、演练、非管理员禁用态）

## 功能

- [x] 三栏微信桌面版 shell，左导航 5 个入口与选中态容器存在
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
- `demo` 租户原库没有用户行，验收前补种了 `u_admin`（admin）与 `u_member`（member）两条 `users` 记录，仅用于本次手工验收，未随仓库提交。
- 压测与评测数字来自本机 mock 模式、已有语料与固定 query，只代表该次环境，不是容量承诺。
