# 文档入库流水线与运营能力设计

- 日期：2026-11-19
- 状态：待评审
- 关联任务：Plan Phase 4（3.6 / 3.7 / 4.1 / 4.2 / 4.3 / 4.4 / 4.5）+ 新增"上传链路"
- 范围决定：M1–M3 全部纳入；入库采用**异步任务队列**；OCR 用 **RapidOCR 本地**；连接器先做**本地目录 + 飞书**

## 1. 目标

把系统从"能检索已有 chunk"补成**能持续吃进文档并运营**的闭环：

```
上传 / 连接器拉取 → 入库任务 → 解析（含表格、扫描件） → 切分 → 嵌入 → 落库 → 发布版本
                                                              ↓
                                               trace 记录 → 运营报表 → 单页工作台展示
```

**成功标准（可验收）**

1. 上传一份 20 页中文 PDF，30 秒内 `job` 从 queued 走到 succeeded，检索能命中其内容；
2. 表格类问题能正确引用到具体单元格；
3. 纯扫描件（无文字层）能被 OCR 识别并入库；
4. 本地目录增量同步：第二次同步不重复入库；
5. 任一请求可通过 trace_id 回看各阶段耗时与命中数；
6. 报表能给出零结果查询 Top、拒答率、知识缺口 Top；
7. 压测报告给出 p50/p95/p99 与降级率；
8. 单页 UI 能完成"选库 → 上传 → 检索 → 流式问答 → 看引用 → 切租户"全流程。

## 2. 架构

新增 2 个进程，复用现有容器：

| 组件 | 形态 | 职责 |
|---|---|---|
| API（现有） | 已有容器 | 收件、查状态、检索、问答、统计、托管 UI |
| **worker（新）** | 同镜像，独立启动命令 | 领任务 → 解析 → 切分 → 嵌入 → 入库 → 更新状态 |
| **parser（已有代码，未验收）** | 独立容器，`profiles: ["parser"]` | PDF 解析 + 表格抽取 + OCR 兜底 |

任务领取用 **`SELECT ... FOR UPDATE SKIP LOCKED`**，不引入 Redis/Celery（少一个组件，单机足够）。

## 3. 数据模型变更（迁移 0009）

`ingest_jobs` 现有字段：`id / tenant_id / kb_id / doc_id / status / error / created_at / updated_at`。

新增：

| 字段 | 类型 | 用途 |
|---|---|---|
| `source` | text default '' | 来源描述（upload / local_dir / feishu） |
| `attempts` | int default 0 | 重试次数，超过 3 次置 failed |
| `progress` | int default 0 | 0–100，供 UI 轮询显示 |
| `finished_at` | timestamptz null | 完成时间，用于统计耗时 |

状态机：`queued → running → succeeded | failed`（failed 可由人工重试回到 queued）。

**约束**：`ingest_jobs` 不加 RLS（本期），但所有查询必须带 `tenant_id`，与现有三层隔离规则一致。

## 4. 接口契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}/documents` | multipart 上传；写文件到 `data/uploads/{tenant}/{kb}/`；返回 **202 + job_id** |
| GET | `/v1/jobs/{job_id}` | 返回 status / progress / error / doc_id |
| GET | `/v1/jobs?kb_id=` | 列表，供 UI 轮询 |
| GET | `/v1/traces/{trace_id}` | 单次请求的阶段耗时与命中详情 |
| GET | `/v1/stats?kb_id=` | 零结果查询 Top N、拒答率、覆盖率、知识缺口 |
| GET | `/ui` | 单页工作台（StaticFiles 托管） |

**安全约束（沿用现有规则）**：租户只从凭证推导；body 不接受 `tenant_id`；`kb_id` 不属于当前租户返回 **404**。

## 5. 解析与切分

1. **优先 parser 容器**；容器不可用 → 本地 pypdf 降级（已实现）；
2. **blocks 为空 → RapidOCR**（同一容器内，模型预置镜像，不运行时下载）；
3. **表格块单独成块**：转 Markdown，**每行重复表头**，`meta.table = true`；
4. 切分复用 `split_document`（Markdown 标题驱动），表格块不参与父子合并，避免表头丢失；
5. 入库后按现有逻辑写 `documents`、`chunks`、`kb_bm25`；是否自动发布版本由请求参数决定（默认不发，保持"发布是显式动作"）。

## 6. 连接器

统一协议：

```python
class Source(Protocol):
    name: str
    def list_documents(self, since: datetime | None) -> Iterable[DocumentRef]: ...
    def fetch(self, ref: DocumentRef) -> bytes: ...
```

- **local_dir**：按 `mtime + 文件哈希` 判断增量；
- **feishu**：按文档 `revision` 判断增量，凭据走环境变量；无凭据时该连接器不注册（启动日志提示），不影响其他功能。

调度：worker 内的定时循环（间隔可配），同一文档的重复任务按 `content_hash` 去重。

## 7. 可观测

- **trace**：每次检索/问答生成 `trace_id`，JSONL 落 `data/traces/YYYY-MM-DD.jsonl` + 内存环形缓冲 1000 条；字段：`trace_id / tenant / kb / stage / ms / hits / refused / mode`；
- **报表**：基于 `ingest_jobs` + traces 聚合（零结果查询、拒答率、覆盖率、Top 缺口），不新增表。

## 8. 压测

脚本（asyncio 并发）打 `/v1/kbs/{kb}/search` 与 `/v1/chat/stream`，输出 p50/p95/p99、错误率、rerank 降级率；报告写入 `eval/loadtest_report.json` 并附 README。

## 9. UI（单文件 HTML）

功能仅 6 项：选库、上传、检索结果（含分数与来源）、流式问答、引用可点击展开原文、租户/角色切换。

## 10. 测试策略

| 层 | 测试 |
|---|---|
| 解析 | 3 份真实 PDF fixture（已生成）；表格转 MD 断言表头重复；扫描件走 OCR 的分支用 mock |
| job | 状态机流转、重试上限、`SKIP LOCKED` 并发不重复领取（集成测试） |
| 上传 | 202 返回、租户隔离 404、非 PDF 拒绝 |
| 连接器 | local_dir 增量同步两次，第二次 0 新增 |
| trace/报表 | 纯函数聚合单测 + 一次集成 |
| UI | 手工验收清单（不做前端自动化） |

## 11. 明确不做

登录鉴权页、知识库 CRUD 后台、分布式队列、实时（webhook）同步、前端框架与构建链。

## 12. 批次

| 批次 | 内容 |
|---|---|
| **P1（合并）** | 迁移 0009 + 上传接口 + job 状态机 + worker + parser 镜像验收 + 表格转 MD |
| P2 | 扫描件 OCR 兜底 |
| P3 | 连接器（Source 协议 + local_dir + feishu） |
| P4 | trace + 报表接口 |
| P5 | 压测 + 降级演练 |
| P6 | 单页 UI |
| P7 | 门面：README 架构图/指标/截图 + ADR 补 2 条 |

## 13. 风险

| 风险 | 应对 |
|---|---|
| `unstructured` 镜像构建失败 | P1 第一步就 build；失败切 `pypdf + pdfplumber`，不影响接口层 |
| RapidOCR 模型体积/下载 | 模型随镜像预置；镜像体积可接受（独立容器） |
| 飞书应用凭据缺失 | 连接器按需注册，缺凭据不阻塞启动 |
| 异步入库与版本发布冲突 | 默认不自动发布；发布仍是显式动作，复用现有指针回滚 |
