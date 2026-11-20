# 调测驾驶舱验收清单

## 环境

- 验收日期：2026-11-20
- commit：本次 Task 15 提交（`docs: complete ops cockpit acceptance and ADR`），工作区基线 `ede6bd2`；提交号以 `git log -1 --oneline` 为准
- 运行模式：mock（`EMBED_MODE=mock`；入库与检索使用确定性哈希向量，界面分数不代表真实检索质量）
- `OPS_ENABLE_ACTIONS`：本次自动验证未改变运行配置；运行中的 `http://127.0.0.1:8000` 容器是旧镜像，`/openapi.json` 只暴露 `/live`、`/ready`、search、chat、documents、jobs、stats、traces，没有 `/v1/ops/*` 与 `/v1/identity`，因此无法在浏览器里验证按钮启用/禁用和端到端触发
- tenant / kb / user：`demo` / `kb_after_sale` / `u_admin`（驾驶舱目标）；自动化冒烟占用 `t_smoke` / `kb_smoke`，不清理既有数据
- 数据库：`127.0.0.1:5433/kb`
- 验证范围：静态页面标记、全量回归、Gate C、P1 入库冒烟、固定压测；浏览器手工点击与三项演练实跑未执行，原因见下方“证据边界”

## 功能

- [x] 三栏微信桌面版 shell，左导航 5 个入口与选中态容器存在
  - 证据：`tests/test_api_ui.py::test_ui_has_wechat_desktop_shell`、`test_ui_shell_navigation_markers`
- [x] 驾驶舱只读面板齐全：健康、队列、任务、任务事件、AI 指标、Trace 列表与瀑布
  - 证据：`tests/test_api_ui.py::test_ui_has_cockpit_panels`
- [x] 身份卡显示 role / tags / 可见 KB / 三层隔离 / 拒绝原因 / 身份对比面板
  - 证据：`tests/test_api_ui.py::test_ui_has_identity_panels`、`tests/test_identity_compare.py`
- [x] 运行历史、运行详情、最近一次评测指标卡与相对上一轮 delta 容器存在
  - 证据：`tests/test_api_ui.py::test_ui_has_run_metrics_and_delta`
- [x] 受控触发区分三种入口：mock 评测直接提交；固定压测二次确认；安全演练只展开三选一子按钮
  - 证据：`app/static/index.html` 中 `run-trigger-eval` / `run-trigger-loadtest` / `run-trigger-drill` 显式绑定，`data-drill="drill_parser|drill_llm|drill_queue"` 独立确认后触发
- [x] 非管理员或 `OPS_ENABLE_ACTIONS` 关闭时，触发按钮、演练按钮与身份对比按钮同步禁用
  - 证据：`updateActionAvailability()` 读取 `identity.ops.can_trigger`，同时管理三枚触发按钮、`[data-drill]`、`identity-compare-button` 与 `updateRetryButton(...)`
- [ ] 浏览器中管理员触发 mock 评测，观察 queued → running → succeeded 并看到指标与 delta
- [ ] 浏览器中固定压测出现 p50 / p95 / p99，三项安全演练可运行并产生审计
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

### 指标口径

- `recall_at_k`：可回答用例中，前 k 条命中的标注 chunk 比例逐题计算后取均值，多标注可部分得分。
- `mrr`：可回答用例中，前 k 条里首个正确结果的倒数排名均值。
- `hit_rate`：可回答用例中 `recall_at_k` 大于 0 的比例。
- `refusal_accuracy`：不可回答用例中正确拒答的比例。
- `answer_coverage`：可回答用例中未拒答的比例；生成失败单独计入 `counts.generation_errors`。
- `citation_accuracy`：可回答、生成成功且未拒答的回答中，引用 chunk_id 存在于实际上下文的比例均值；无引用回答按 0 计入分母。

### 证据边界

- 运行中的 API 容器未包含本仓库 `/v1/ops/*` 与 `/v1/identity` 路由，ops-runner 也未启动；因此“管理员触发 → queued/running/succeeded”“压测 p50/p95/p99 在界面展示”“三项演练实跑”“按钮禁用态”均未在浏览器确认，不能凭静态标记勾选。
- 复跑手工验收需要：重建并启动包含当前代码的 API 与 worker，注入 `OPS_ENABLE_ACTIONS=1`，启动 `scripts/run_ops.py`，再用管理员身份打开 `/ui`。
- 压测数字来自本机 mock 模式、已有语料与固定 query，只代表该次环境，不是容量承诺。
