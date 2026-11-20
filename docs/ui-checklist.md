# UI 验收清单

## 验收信息

- 验收日期：2026-11-19
- 验收 URL：http://127.0.0.1:8000/ui/
- 验收 commit：`949d18c578b0a6b261bae49bc280a53b5052ed63`（Task 19 当前 HEAD）
- 验收方式：真实浏览器手工验收，未新增前端自动化。
- 服务状态：Docker API、parser、db 均为 healthy，worker 运行。
- 非密钥运行配置：`EMBED_MODE=mock`；`REFUSAL_MIN_SIMILARITY=0.18`（按项目 mock 标定）。问答使用项目本地既有 LLM 配置，本清单不记录任何密钥。
- 指标边界：下列 score 是 mock 模式下本次界面的实际展示，不代表真实 bge-m3 检索质量。

## 功能验收

- [x] **选库/页面**
  - 操作：打开验收 URL，检查页面、默认租户与 KB，随后将 KB 改为 `kb_smoke`。
  - 实际可见结果：标题为“知识运维工作台”，六个面板齐全；默认 tenant 为 `demo`、KB 为 `kb_after_sale`，之后成功改为 `kb_smoke`。
  - 截图：[`ui-overview-top.jpg`](images/ui-overview-top.jpg)

- [x] **检索**
  - 操作：在 `kb_after_sale` 中查询“退款一般几天到账”。
  - 实际可见结果：返回 2 条，状态为“检索完成 · 2 条命中 · hybrid · KB kb_after_sale”。#1 为 `kb-after-sale#1`，score 0.016393，doc_id `kb-after-sale`；#2 为 `kb-after-sale#2`，score 0.016129。
  - 截图：[`ui-search-results.jpg`](images/ui-search-results.jpg)

- [x] **流式问答**
  - 操作：针对同一问题发起流式问答。
  - 实际可见结果：状态为“回答完成 · 收到 [DONE] · 2 条引用”；正文为“退款审核通过后按原支付路径退回，通常 1-3 个工作日到账 [kb-after-sale#1]。”
  - 截图：[`ui-streaming-qa.jpg`](images/ui-streaming-qa.jpg)

- [x] **引用展开**
  - 操作：点击引用 `[kb-after-sale#1]`。
  - 实际可见结果：展示 doc_id、score、真实退款正文及 metadata，metadata 包含 title、audience、source_url、section_path、route、vector_score。
  - 截图：[`ui-reference-expand.jpg`](images/ui-reference-expand.jpg)

- [x] **角色/上下文**
  - 操作：将成员切换为用户 `u_member`、管理员切换为 `u_admin`，再将访客恢复为 `demo-user`。
  - 实际可见结果：scope 预览同步更新，并提示旧轮询、检索、流停止，历史和原文缓存清空。
  - 截图：[`ui-role-admin.jpg`](images/ui-role-admin.jpg)

- [x] **租户/上传**
  - 操作：切换到 tenant `t_smoke`、KB `kb_smoke`，上传 `policy_single.pdf`。
  - 实际可见结果：最终 progress 为 100%，状态为 succeeded，任务 `#4093`，文档 `kb_smoke-b8f86c49`，来源 upload，attempts 为 1。
  - 截图：[`ui-upload-complete.jpg`](images/ui-upload-complete.jpg)

## 控制台与网络

- 控制台消息为空。
- 本流程网络请求均为 200/202，无失败请求。

## 角色权限边界

- 角色/上下文项仅记录预设身份切换及其 UI 表现，不构成 ACL 提权验证；真实权限由后端用户表决定。
