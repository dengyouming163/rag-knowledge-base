# 评测集说明（cases.jsonl）

## 用途

这是项目唯一的"标尺"。任何改动（切分、embedding、检索、rerank、prompt、模型）都必须先跑这份集，再决定是否合入。

## 字段

| 字段 | 说明 |
|---|---|
| `case_id` | 唯一编号，`c001` 起顺序分配，**不复用、不重排** |
| `kb_id` | 所属知识库，当前全部为 `kb_demo` |
| `query` | 用户真实问法，允许口语、错别字、省略 |
| `expected_chunk_ids` | 标注的正确 chunk；不可回答题为空数组 `[]` |
| `answer` | 参考答案，必须能从 chunk 原文推出，不得引入外部知识 |
| `answerable` | 文档是否有答案；`false` 表示应当拒答 |
| `type` | `fact` / `compare` / `multi_hop` / `unanswerable` |
| `difficulty` | `easy` / `medium` / `hard` |
| `adversarial` | 可选，`true` 表示口语化、边界值或诱导式问法 |

## 标注口径

1. **正确 chunk 判定**：读该 chunk 能独立回答该问题，才算命中。需要跨两个 chunk 才能回答的，两个都标。
2. **不可回答判定**：语料全部读完仍找不到依据，才标 `unanswerable`。不要因为"当前检索不到"就标成不可回答——那是系统问题，不是数据问题。
3. **答案写法**：只用文档里的信息，数字、期限、动作必须与原文一致；允许压缩表达，不允许补全原文没说的内容。
4. **多跳题**：必须真的需要两个 chunk，不能是"两问拼一句"的伪多跳。
5. **对抗题**：口语（"钱什么时候能回来"）、边界（"第八天"）、诱导（"是不是必须原包装"）。诱导题的正确答案经常是"文档没说"，不要强行给答案。

## 当前分布（共 50 条）

| 类型 | 数量 | 说明 |
|---|---|---|
| fact | 25 | 含 4 条对抗问法 |
| compare | 8 | 单 chunk 内对比 4 条，跨 chunk 对比 4 条 |
| multi_hop | 5 | 均需 2 个 chunk |
| unanswerable | 12 | 发票、保修、积分、海外、安装、价格、颜色、退货地址、客服电话、支付、优惠券、企业采购 |

## 语料规模（demo_corpus.json）

当前 26 个 chunk。首版只有 7 个 chunk 时，top-5 从 7 条里选 5 条，Recall 直接满分，**没有区分度**。因此扩充到 26 条（新增产品参数、更多错误码、日常维护），并严格避开上述 12 个不可回答主题。

判断标准：如果某个指标长期接近 1.0，先怀疑评测集太简单，而不是系统太好。

## 维护规则

- 新增用例追加到文件末尾，`case_id` 顺延。
- 修改已有用例必须提升 `version`（见下）并在提交信息里说明原因。
- 任何"为了让它通过而改标注"的行为都视为作弊，必须回滚。
- 语料扩充后，同步补充该语料的 3-5 条用例。

## 版本

- `v1`（2026-11-18）：首版 50 条，基线为 numpy + BM25 混合检索。

## 压测报告（loadtest_report.json）

先启动 API，并确认目标租户、知识库及可用语料已存在，再运行：

```bash
py -3.13 scripts/loadtest.py --concurrency 20 --requests 200
```

可用 `--base-url`、`--kb`、`--tenant`、`--user-id`、`--query`、`--top-k`、`--mode`、`--audience`、`--timeout` 覆盖运行条件，用 `--out` 修改输出路径；默认写入 `eval/loadtest_report.json`。`--requests` 是**每个端点各自的请求数**：search 与 chat 各 200 个，默认合计 400 个；`--concurrency` 是两类请求共享的全局 worker 上限。

指标口径：

- 延迟从请求开始计到完整响应或 SSE 流结束，**成功和失败请求均计入** p50/p95/p99，单位毫秒。percentile 使用升序样本 `(n-1)*q` 位置的线性插值。
- `error_rate = failed / total`。HTTP 非 2xx、连接或超时异常、SSE `error` 事件、无效/缺失 `hits`、缺少 `[DONE]` 都计为失败；失败样本不会被丢弃。
- `rerank_degradation_rate = rerank_degraded / rerank_observed`，分母仅为**成功且包含有效 `hits` 事件的 chat 响应**。search 不参与，失败 chat 也不参与，避免稀释降级率。
- 空样本的比率和 percentile 均为 `null`，不伪造 0 ms。报告保留全部错误分类计数，并最多列出每端点前 20 条错误样本。
