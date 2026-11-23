# 企业 RAG 知识库：P0 工程补齐、GitHub 专业化与演示部署 设计规格

- 日期：2026-11-23
- 状态：已批准（待实现）
- 关联：`docs/superpowers/specs/2026-11-23-storage-view-design.md`（存储视图，已完成）

## 1. 背景与目标

项目已具备完整 RAG 闭环（异步入库、混合检索、拒答与引用、流式问答、运营控制台、存储视图），
但作为"企业级 GitHub 项目 + 可演示的线上环境"仍有四项缺口：

1. 工程 P0 短板：无鉴权、无限流/CORS、无 CI、向量模型漂移无检测、存在未提交 WIP、worker 缺少租约回收。
2. GitHub 仓库不符合专业开源项目惯例（缺 LICENSE/CI/模板/规范）。
3. 没有可公开访问的演示环境。
4. 文档未按"企业 RAG / 企业 AI 基础设施"视角组织，且无英文版本。

本规格定义一次交付，使项目达到"可公开演示 + 可作为作品/方案展示"的状态。

## 2. 范围

### 2.1 本次范围（In Scope）

| 工作流 | 内容 |
|---|---|
| A | P0 工程补齐（演示级鉴权、CORS、限流、上传上限、向量漂移检测与重嵌工具、worker 租约回收、WIP 收口） |
| B | GitHub 专业化（LICENSE、CI、模板、规范、badges、目录整理） |
| C | 生产演示部署（独立端口 + Basic Auth、独立 compose project、预置语料、运维脚本） |
| D | 文档重写（英文主 + 中文镜像，企业视角，含架构图与截图） |

### 2.2 不在本次范围（Out of Scope）

- 完整 JWT/OIDC 鉴权、RBAC 管理后台（保持 Phase 3 规划）。
- HTTPS/域名接入（演示用 HTTP；后续可加）。
- Prometheus/Grafana 告警体系、日志聚合（仅保留既有 trace/metrics 能力）。
- 前端构建体系（ESLint/Vite）与自动化 E2E。
- 评测集大规模扩充（沿用现有 50 条 + README 说明）。
- 多 worker 并发与 BM25 分布式重建。

## 3. 已锁定决策

| 决策 | 取值 |
|---|---|
| 演示鉴权 | nginx 边车 Basic Auth + 限流；api/db/minio 不发布主机端口 |
| 演示入口 | 独立端口 `18000` + HTTP，形如 `http://<host>:18000/ui/` |
| AI 与语料 | 真实 LLM（DeepSeek）+ 真实 embedding（bge-m3）；预置售后政策 + 16 篇法律 docx |
| 文档语言 | `README.md` 英文；`README.zh-CN.md` 中文镜像；`docs/` 英文重写 |
| License | Apache-2.0 |

## 4. 工作流 A：P0 工程补齐

### A1 演示级鉴权网关（nginx 边车）

- 新增 `deploy/nginx/demo.conf`：
  - `auth_basic` 保护 `/ui` 与 `/v1`；`/health` 例外。
  - `limit_req_zone` 按 IP 限流（如 10 r/s，burst 20）。
  - `client_max_body_size 50m`。
  - 反代到 `api:8000`；保留 `X-Forwarded-*`；关闭 `/ui` 缓存。
  - gzip 静态资源。
- 新增 `deploy/docker-compose.demo.yml`：
  - `gateway` 发布 `18000:80`；`api`/`db`/`minio`/`worker` 仅内部网络。
  - `restart: unless-stopped`；日志 `max-size=10m, max-file=3`。
  - 独立卷/网络/项目名 `ragkb-demo`。
- 凭据由部署脚本生成，写入服务器 `.env`（600 权限），不进入仓库。

**验收**：未带凭据访问 `/ui/` 返回 401；带凭据可加载；`/health` 免鉴权；db/minio 端口不可从公网访问。

### A2 CORS 中间件

- `app/main.py` 增加 env 驱动的 `CORSMiddleware`：
  - `CORS_ALLOW_ORIGINS`（逗号分隔，默认空 = 不启用跨域）。
- 不改变现有同源行为。

**验收**：默认配置下无 CORS 头；设置白名单后预检通过；既有测试全绿。

### A3 上传大小上限

- 上传接口读取 `MAX_UPLOAD_BYTES`（默认 50MB），超限返回 413。
- 与 nginx 的 `client_max_body_size` 双重保护。

**验收**：超过阈值的上传被拒绝且不落盘；正常文件不受影响；补单测。

### A4 向量漂移检测与重嵌工具

- 新增 `scripts/reembed_chunks.py`（由本次临时脚本产品化）：
  - 按 `embed_model_version` 选择目标块；分批嵌入；幂等可续跑。
  - `--tenant/--kb/--only/--limit/--dry-run` 参数。
- 新增 `GET /v1/ops/storage/embedding-drift`：返回按 KB 的模型版本分布与漂移块数（只读）。
- 启动时若检测到漂移，打印一次 WARN（不做自动重嵌）。

**验收**：脚本对已一致数据为 no-op；`--dry-run` 不写库；接口返回结构有测试。

### A5 worker 租约回收

- 检查 `src/ingest_jobs.py` 对 `running` 状态的处理：
  - 若任务卡在 `running` 超过 `JOB_LEASE_SECONDS`（默认 900s），允许重新入队/回收。
  - 保留 `MAX_ATTEMPTS=3` 语义；超过则终态失败。
- 补测试覆盖"卡死任务被回收"。

**验收**：模拟超时任务后能被再次领取；正常任务不受影响。

### A6 未提交 WIP 收口

- 审查工作区现有 8 个已修改 + 9 个未跟踪文件，按主题分批提交：
  1. 拒答阈值与 pipeline 变更（`src/refusal.py`、`src/pipeline.py`、`src/retrieve.py`、相关测试）。
  2. PDF 解析包（`src/parsers/`、`tests/test_pdf_parser.py`、fixtures 生成脚本）。
  3. 评测生成链路（`scripts/run_eval.py`、`tests/test_eval_generate.py`、`eval/baseline_gen.json`、`eval/baseline_real_gen.json`）。
  4. observability 计划文档与 `eval/` 数据。
- 每个提交前运行对应测试；不得夹带无关改动。

**验收**：`git status` 干净；全量 pytest + node 测试绿。

## 5. 工作流 C：演示部署

### 5.1 拓扑

```
Internet :18000 → gateway(nginx: BasicAuth + limit_req)
                   └→ api:8000  (FastAPI + /ui 静态控制台)
                        ├→ db:5432   (pgvector, 内部网络, 卷)
                        ├→ minio:9000 (内部网络, 卷)
                        └→ worker (同一镜像, 无端口)
```

### 5.2 服务器约束（已探测）

- TencentOS 3.3（RHEL 系）、4C / 7.5G / 28G 可用、Docker 26 + Compose v2。
- **80/443 已被其他业务占用**；服务器为共享环境。
- firewalld 开启。

### 5.3 部署步骤（脚本化 `scripts/deploy_demo.sh`）

1. 传输代码：优先 `git clone` 仓库；无推送凭据时用 `git bundle` 上传。
2. 生成 `.env`：DB/MiniIO 随机密码、演示基本认证凭据、`LLM_*`/`EMBED_*`（真实 Key）。
3. `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml up -d --build`。
4. `alembic upgrade head`（通过 api 容器执行）。
5. 预置语料：创建 demo 租户/KB，上传售后政策与 16 篇法律 docx，等待 `ready`。
6. firewalld 放行 `18000/tcp`（永久）。
7. 健康检查：`/health`、`/v1/storage`、`/ui/`。
8. 输出演示地址与账号。

### 5.4 安全与运维

- `.env` 权限 600；不提交仓库。
- 上传限流 + 大小限制；演示账号允许上传但受限。
- 日志轮转；提供 `pg_dump` 备份与回滚说明（镜像 tag + 数据卷）。
- 部署完成后建议轮换 root 密码、改用 SSH key。

**验收**：公网可访问并可完成"上传 → pending → ready → 存储视图可见 → 实验室问答有引用"的全流程。

## 6. 工作流 B：GitHub 专业化

### 6.1 新增根文件

`LICENSE`（Apache-2.0）、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`CHANGELOG.md`、
`.editorconfig`、`.gitattributes`（LF）、`.env.example`；`ruff` 配置写入 `pyproject.toml`。

### 6.2 `.github/`

- `workflows/ci.yml`：
  - `python`: 启动 `pgvector/pgvector:pg16` service → `alembic upgrade head` → `pytest`（含覆盖率上报，暂不设硬阈值）→ `ruff check`。
  - `frontend`: `node --test` 全量 JS 测试。
  - `eval`: `scripts/run_eval.py --mock --compare eval/baseline.json` 作为回归门禁。
- `ISSUE_TEMPLATE/bug_report.yml`、`feature_request.yml`、`PULL_REQUEST_TEMPLATE.md`、`dependabot.yml`。

### 6.3 规范

- Conventional Commits；README 顶部 badges（CI、Python、License）。
- 建议在 GitHub 开启 `main` 分支保护（需仓库管理员操作，仓库内只提供说明）。

**验收**：CI 三个 job 全绿；badge 可点击；模板生效。

## 7. 工作流 D：文档体系

### 7.1 文件清单

| 文件 | 要点 |
|---|---|
| `README.md`（EN） | 定位、特性、Mermaid 架构图、Quickstart、Live Demo、API 摘要、评测指标、Roadmap、License |
| `README.zh-CN.md` | 与英文镜像一致 |
| `docs/ARCHITECTURE.md` | 分层、组件职责、数据流、存储/索引设计、隔离模型 |
| `docs/DEPLOYMENT.md` | 本地与生产部署、compose、env、迁移、备份/回滚 |
| `docs/OPERATIONS.md` | runbook：健康检查、常见故障、重嵌、任务回收 |
| `docs/SECURITY.md` | 信任边界、RLS/ACL、注入防护、演示环境的安全说明 |
| `docs/EVALUATION.md` | 评测方法、指标、本次真实结果、已知边界与局限 |
| `docs/API.md` | 端点概览（指向 OpenAPI `/docs`） |
| `docs/ROADMAP.md` | P1/P2 规划（JWT、HTTPS、可观测性、评测扩充） |
| `docs/README.md` | 文档索引 |
| `docs/assets/screenshots/` | 概览、实验室问答、存储视图 3 张截图 |

### 7.2 写作要求

- 企业视角：强调多租户隔离、可靠性、可运维性、可评测性，避免营销化措辞。
- 全部使用可验证事实（指标来自 `docs/EVALUATION.md` 实测数据）。
- 演示地址与截图在部署成功后回填。

**验收**：所有链接有效；中英文内容一致；无占位符/TODO。

## 8. 执行顺序

1. A（工程补齐：A1→A6，其中 A6 先做，避免与后续改动冲突）
2. 全量测试回归
3. C（部署 + 预置语料 + 截图）
4. B（仓库文件 + CI）
5. D（文档重写 + 回填演示地址）
6. 最终验收与提交

依赖关系：C 依赖 A 的 compose/gateway；D 依赖 C 的地址与截图；B 的 CI 依赖 A6 后的干净工作区。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 共享服务器误伤既有业务 | 独立 project/网络/卷/端口；绝不 `down` 其他项目；上线前用 `docker ps` 核对 |
| 磁盘偏紧（28G） | 清理 build cache；镜像用 slim；限制日志 |
| 真实 Key 被滥用 | Basic Auth + 限流 + 上传上限 |
| GitHub 推送无凭据 | 优先尝试推送；失败则用 bundle 部署，推送由用户提供 token 后完成 |
| root 密码已在对话中出现 | 部署后建议立即改密并换 SSH key；`.env` 600 |
| CI 依赖 Postgres | 使用 pgvector service 容器；数据库不可用时测试会 skip，CI 中强制要求可用 |

## 10. 端到端验收清单

1. `git status` 干净；全量 `pytest` 与 `node --test` 全绿。
2. CI 三个 job 绿。
3. `http://<host>:18000/health` 可访问；未鉴权 `/ui/` 返回 401。
4. 鉴权后 UI 可加载，`#/storage` 正常，实验室提问返回带引用的回答。
5. 上传 `.docx` 后 `pending → ready`，存储视图显示文件名/大小/状态。
6. 向量漂移接口返回 0 漂移；重嵌脚本可幂等运行。
7. README 双语齐全，Live Demo 链接可用，截图存在。
8. LICENSE/CI/模板/CHANGELOG 齐全。

## 11. 后续（不在本次）

JWT/OIDC 鉴权、HTTPS 与域名、Prometheus 与告警、前端构建与 E2E、评测集扩充、多 worker 并发。
