# 文档入库流水线 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 补完「上传/连接器 → 异步入库 → 解析（表格、扫描件） → 切分嵌入 → 落库 → trace/报表 → 单页 UI」全链路。

**Architecture:** API 只收件并写 `ingest_jobs`；worker 进程用 `SELECT ... FOR UPDATE SKIP LOCKED` 领任务；重依赖（unstructured / RapidOCR）只活在 `parser` 容器；主服务通过 HTTP 调解析器，失败降级本地 pypdf。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / Alembic / Postgres 16 + pgvector / httpx / pytest；parser 容器 Python 3.11 + unstructured + RapidOCR。

**Spec:** `docs/superpowers/specs/2026-11-19-ingest-pipeline-design.md`

**执行约定：**
- 所有命令在 `projects/rag-knowledge-base` 下执行，用 `py -3.13`
- 每完成一个 Task 必须：新增测试 → 全量 `py -3.13 -m pytest -q` 通过 → 单独 commit
- 不手写 SQL 改表结构，只通过 Alembic
- 所有查询必须带 `tenant_id`；body 不接受 `tenant_id`

---

## P1（合并批次）：入库链路打通 + 解析验收 + 表格

### Task 1: 迁移 0009 扩展 ingest_jobs

**Files:**
- Create: `migrations/versions/0009_ingest_job_fields.py`
- Modify: `src/db/models.py`（`IngestJob` 类，约 201–220 行）
- Test: `tests/test_ingest_job_model.py`

**Step 1: 写失败测试**

```python
from src.db.models import IngestJob


def test_ingest_job_has_progress_fields() -> None:
    columns = set(IngestJob.__table__.c.keys())
    assert {"source", "attempts", "progress", "finished_at"} <= columns
    assert IngestJob.__table__.c.attempts.default.arg == 0
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ingest_job_model.py -q`
Expected: FAIL（`KeyError`/断言失败，字段不存在）

**Step 3: 实现**

模型新增：

```python
    source: Mapped[str] = mapped_column(Text, nullable=False, default="upload", server_default="")
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    progress: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

迁移（全部用 `op.execute`，仓库既有风格）：

```python
def upgrade() -> None:
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS progress integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS finished_at timestamptz")


def downgrade() -> None:
    for column in ("source", "attempts", "progress", "finished_at"):
        op.execute(f"ALTER TABLE ingest_jobs DROP COLUMN IF EXISTS {column}")
```

**Step 4: 应用迁移并验证**

Run: `py -3.13 -m alembic upgrade head && py -3.13 -m pytest tests/test_ingest_job_model.py tests/test_db_models.py -q`
Expected: PASS（`test_migration_applied` 动态读 head，无需改断言）

**Step 5: Commit**

```bash
git add migrations/versions/0009_ingest_job_fields.py src/db/models.py tests/test_ingest_job_model.py
git commit -m "feat(ingest): add job progress fields via migration 0009"
```

---

### Task 2: job 状态机（入队 / 领取 / 完成 / 失败 / 重试）

**Files:**
- Create: `src/ingest_jobs.py`
- Test: `tests/test_ingest_jobs.py`

**Step 1: 写失败测试**

```python
def test_claim_is_idempotent_under_concurrency(engine):
    """两个 worker 同时领，只能有一个拿到同一个 job。"""
    job_id = enqueue(engine, TENANT, KB, doc_id="d1", source="upload")
    first = claim_next(engine, worker="w1")
    second = claim_next(engine, worker="w2")
    assert first["id"] == job_id
    assert second is None or second["id"] != job_id


def test_failure_after_three_attempts_marks_failed(engine):
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    for _ in range(3):
        job = claim_next(engine, worker="w1")
        mark_failed(engine, job["id"], "boom")
    assert get_job(engine, TENANT, job_id)["status"] == "failed"
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ingest_jobs.py -q`
Expected: FAIL（模块不存在）

**Step 3: 实现**

```python
STATUSES = ("queued", "running", "succeeded", "failed")
MAX_ATTEMPTS = 3


def enqueue(engine, tenant_id: str, kb_id: str, doc_id: str, *, source: str = "upload") -> int:
    """写一条 queued；同一 (tenant, kb, doc_id) 已有排队/运行中的任务时直接复用。"""
    # 逻辑：
    # 1) SELECT id FROM ingest_jobs WHERE tenant_id=? AND kb_id=? AND doc_id=?
    #       AND status IN ('queued','running')
    # 2) 有 → 返回该 id（避免重复入队）
    # 3) 无 → INSERT (status='queued', progress=0) RETURNING id


def claim_next(engine, *, worker: str = "w1") -> dict | None:
    """领一条：tenant_scope 内 SELECT ... FOR UPDATE SKIP LOCKED，然后置 running。"""
    # SQL 要点：
    #   SELECT id, tenant_id, kb_id, doc_id, attempts
    #     FROM ingest_jobs WHERE status = 'queued'
    #     ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1
    #   UPDATE ingest_jobs SET status='running', progress=10, attempts=attempts+1, updated_at=now()
    #     WHERE id = :id RETURNING *


def mark_progress(engine, job_id: int, progress: int) -> None: ...


def mark_succeeded(engine, job_id: int) -> None:
    """status='succeeded', progress=100, finished_at=now(), error=''"""


def mark_failed(engine, job_id: int, error: str) -> None:
    """attempts < MAX_ATTEMPTS → 回到 queued（可重试）；否则 status='failed'。"""


def get_job(engine, tenant_id: str, job_id: int) -> dict | None: ...
def list_jobs(engine, tenant_id: str, kb_id: str, limit: int = 20) -> list[dict]: ...
```

**关键实现细节（必须照做）：**
- `claim_next` 必须在一个事务里完成"选中 + 置 running"，否则两个 worker 会领到同一条；
- `mark_failed` 超过重试上限要写 `finished_at`，避免报表统计不到结束时间；
- 所有函数都必须显式接收 `tenant_id`（除 `claim_next`，它取出后再校验）。

**Step 4: 跑测试**

Run: `py -3.13 -m pytest tests/test_ingest_jobs.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add src/ingest_jobs.py tests/test_ingest_jobs.py
git commit -m "feat(ingest): add job state machine with skip-locked claiming"
```

---

### Task 3: 上传接口（收件 + 入队）

**Files:**
- Create: `app/routers/documents.py`
- Modify: `app/main.py`（注册路由）、`app/schemas.py`（新增 `UploadResponse`）
- Test: `tests/test_api_documents.py`

**Step 1: 写失败测试**

```python
def test_upload_pdf_returns_202_and_job(client, sample_pdf_bytes):
    resp = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Tenant-Id": TENANT},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"] > 0 and body["doc_id"]


def test_upload_to_other_tenant_kb_returns_404(client, sample_pdf_bytes):
    resp = client.post(
        "/v1/kbs/kb_of_other/documents",
        files={"file": ("a.pdf", sample_pdf_bytes, "application/pdf")},
        headers={"X-Tenant-Id": TENANT},
    )
    assert resp.status_code == 404


def test_upload_rejects_non_pdf(client):
    resp = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("a.exe", b"MZ", "application/octet-stream")},
        headers={"X-Tenant-Id": TENANT},
    )
    assert resp.status_code == 422
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_documents.py -q`
Expected: FAIL（404 路由不存在）

**Step 3: 实现**

```python
ALLOWED_SUFFIX = {".pdf", ".md", ".txt"}
UPLOAD_ROOT = Path("data/uploads")


@router.post("/v1/kbs/{kb_id}/documents", status_code=202)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    tenant_id: str = Depends(current_tenant),
    engine=Depends(get_engine),
):
    # 1) 校验 kb 归属：查 knowledge_bases WHERE id=? AND tenant_id=?，无 → HTTPException(404)
    # 2) 校验后缀白名单 → 422
    # 3) content = await file.read()；空 → 422
    # 4) doc_id = f"{kb_id}-{uuid4().hex[:8]}"
    # 5) 落盘 data/uploads/{tenant_id}/{kb_id}/{doc_id}{suffix}
    # 6) 写 documents 行（status='pending', content_hash=sha256(content)[:16]）
    # 7) job_id = ingest_jobs.enqueue(...)
    # 8) return {"job_id": job_id, "doc_id": doc_id}
```

**安全约束（必须）：** `tenant_id` 只从 `current_tenant` 依赖取，**不从 body/query 读**。

**Step 4: 跑测试**

Run: `py -3.13 -m pytest tests/test_api_documents.py -q`
Expected: PASS

**Step 5: Commit**

```bash
git add app/routers/documents.py app/main.py app/schemas.py tests/test_api_documents.py
git commit -m "feat(api): add async document upload endpoint"
```

---

### Task 4: 任务查询接口

**Files:**
- Create: `app/routers/jobs.py`
- Test: `tests/test_api_jobs.py`

**Step 1: 写失败测试**

```python
def test_get_job_scoped_to_tenant(client):
    # 用别的租户 header 查同 id → 404
    resp = client.get(f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": "other"})
    assert resp.status_code == 404


def test_list_jobs_returns_latest_first(client):
    body = client.get(f"/v1/jobs?kb_id={KB}", headers={"X-Tenant-Id": TENANT}).json()
    assert len(body["jobs"]) >= 1
    assert body["jobs"][0]["status"] in {"queued", "running", "succeeded", "failed"}
```

**Step 2: 跑测试确认失败** → `py -3.13 -m pytest tests/test_api_jobs.py -q`（FAIL: 404）

**Step 3: 实现**

```python
@router.get("/v1/jobs/{job_id}")
def get_job_detail(job_id: int, tenant_id=Depends(current_tenant), engine=Depends(get_engine)):
    job = ingest_jobs.get_job(engine, tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/v1/jobs")
def list_jobs(kb_id: str, tenant_id=Depends(current_tenant), engine=Depends(get_engine)):
    return {"jobs": ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=20)}
```

**Step 4: 跑测试** → PASS

**Step 5: Commit**

```bash
git add app/routers/jobs.py app/main.py tests/test_api_jobs.py
git commit -m "feat(api): expose ingest job status endpoints"
```

---

### Task 5: 入库执行器（解析 → 切分 → 嵌入 → 落库）

**Files:**
- Create: `src/ingest.py`
- Test: `tests/test_ingest.py`

**Step 1: 写失败测试（全程假组件，不联网）**

```python
def test_ingest_markdown_end_to_end(tmp_path, fake_engine, fake_embed):
    path = tmp_path / "policy.md"
    path.write_text("# 售后政策\n\n## 退款\n\n支持七天无理由退款。", encoding="utf-8")

    result = ingest_document(
        fake_engine, tenant_id=TENANT, kb_id=KB, doc_id="d1",
        path=path, embed_client=fake_embed,
    )

    assert result["chunks"] >= 1
    assert result["doc_status"] == "ready"


def test_ingest_pdf_uses_parser_client(tmp_path, fake_engine, fake_embed, fake_parser):
    path = tmp_path / "policy.pdf"
    path.write_bytes(b"%PDF-1.4")
    result = ingest_document(..., parser=fake_parser)
    assert fake_parser.called is True
    assert result["chunks"] >= 1
```

**Step 2: 跑测试确认失败** → `py -3.13 -m pytest tests/test_ingest.py -q`（FAIL: 模块不存在）

**Step 3: 实现**

```python
def ingest_document(engine, *, tenant_id, kb_id, doc_id, path, embed_client, parser=None) -> dict:
    """解析 → 拼 Markdown → 切分 → 批量嵌入 → upsert chunks/kb_bm25。

    返回 {"chunks": int, "doc_status": "ready", "blocked": int}
    """
    # 1) 按后缀选择：.pdf → parser.parse(bytes) → blocks_to_text(blocks)
    #                  .md/.txt → 直接读文本
    # 2) 表格块：table 类型单独转 Markdown（Task 7 的函数），不并入正文段落
    # 3) split_document(text, doc_id=doc_id) → chunks
    # 4) embed_client.embed_texts([c.text for c in chunks 非父块])
    # 5) PgVectorStore(engine, tenant_id, kb_id).upsert(chunks, vectors)
    # 6) BM25Store(engine).rebuild(tenant_id, kb_id)
    # 7) UPDATE documents SET status='ready' WHERE id=doc_id AND tenant_id=?
```

**必须遵守：**
- 父块不参与 embedding（传 `None`），与既有 `upsert` 契约一致；
- 重复导入同一文档先 `DELETE FROM chunks WHERE doc_id=? AND tenant_id=?`，避免 `chunk_id` 主键冲突；
- 任何异常向上抛，由 worker 调 `mark_failed`，不在 `ingest_document` 里吞掉。

**Step 4: 跑测试** → PASS

**Step 5: Commit**

```bash
git add src/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): implement document ingestion executor"
```

---

### Task 6: worker 进程

**Files:**
- Create: `scripts/run_worker.py`
- Modify: `deploy/docker-compose.yml`（新增 `worker` 服务，共享 api 镜像）
- Test: `tests/test_ingest_jobs.py`（补一条 `worker_once` 测试）

**Step 1: 写失败测试**

```python
def test_worker_once_processes_one_job(engine, tmp_path, monkeypatch):
    """领一条、执行、置 succeeded；没有任务时返回 0。"""
    job_id = enqueue(engine, TENANT, KB, doc_id="d1", source="upload")
    assert worker_once(engine) == 1
    assert get_job(engine, TENANT, job_id)["status"] == "succeeded"
    assert worker_once(engine) == 0
```

**Step 2: 跑测试确认失败** → FAIL（`worker_once` 不存在）

**Step 3: 实现**

```python
def worker_once(engine, *, idle_exit: bool = False) -> int:
    """跑一轮：领到就执行并返回 1；没领到返回 0。"""
    # claim_next → ingest_document → mark_succeeded / mark_failed


def main() -> int:
    # 参数：--interval 5 --once
    # 循环 worker_once；Ctrl+C 优雅退出
```

compose 新增（复用 api 镜像，不新增构建）：

```yaml
  worker:
    build:
      context: ..
      dockerfile: Dockerfile
      args:
        PIP_INDEX_URL: https://pypi.tuna.tsinghua.edu.cn/simple
    container_name: rag-kb-worker
    command: ["python", "scripts/run_worker.py", "--interval", "5"]
    environment:            # 与 api 保持一致
      DB_HOST: db
      DB_PORT: "5432"
      DB_USER: kb
      DB_PASSWORD: kb
      DB_NAME: kb
      PARSER_BASE_URL: http://parser:8000
    depends_on:
      db:
        condition: service_healthy
```

**Step 4: 跑测试 + 校验 compose**

Run:
```bash
py -3.13 -m pytest tests/test_ingest_jobs.py -q
docker compose -f deploy/docker-compose.yml config --services
```
Expected: PASS；输出包含 `db api worker`（parser 仍需 profile）

**Step 5: Commit**

```bash
git add scripts/run_worker.py deploy/docker-compose.yml tests/test_ingest_jobs.py
git commit -m "feat(worker): add background ingest worker process"
```

---

### Task 7: parser 容器验收（3 份真实 PDF）

**Files:**
- 已有：`parser/Dockerfile`、`parser/main.py`、`eval/fixtures/pdf/*.pdf`、`scripts/make_pdf_fixtures.py`

**Step 1: 构建镜像（后台）**

```bash
docker compose -f deploy/docker-compose.yml --profile parser build parser
```

**Step 2: 起容器并验健康**

```bash
docker compose -f deploy/docker-compose.yml --profile parser up -d parser
curl http://127.0.0.1:8100/health
```

Expected: `{"status":"ok"}`

**Step 3: 逐份解析 3 个样本**

```bash
curl -s -F "file=@eval/fixtures/pdf/policy_single.pdf" http://127.0.0.1:8100/parse
curl -s -F "file=@eval/fixtures/pdf/policy_table.pdf"  http://127.0.0.1:8100/parse
curl -s -F "file=@eval/fixtures/pdf/policy_two_col.pdf" http://127.0.0.1:8100/parse
```

Expected（三条都满足）：
- `blocks` 数组非空；
- 中文无乱码（出现"售后政策/退款/换货"等词）；
- table 样本至少含一个 `type: "table"` 的块。

**Step 4: 失败预案（必须执行，不能跳过判断）**

若构建失败，记录失败点，改用 **pypdf + pdfplumber** 方案：
- `parser/requirements.txt` 换成 `pypdf / pdfplumber / fastapi / uvicorn / python-multipart`
- `parser/main.py` 用 `pdfplumber` 抽文字 + `extract_tables()` 产出 table 块
- 客户端无需改动（协议不变）

**Step 5: Commit（仅在改了 parser 代码时）**

```bash
git add parser/
git commit -m "fix(parser): switch to pdfplumber pipeline"
```

---

### Task 8: 表格转 Markdown（每行重复表头）

**Files:**
- Create: `src/tables.py`
- Modify: `src/parsers/pdf.py`（`blocks_to_text` 支持 table 块）
- Test: `tests/test_tables.py`

**Step 1: 写失败测试**

```python
def test_table_keeps_header_on_each_row() -> None:
    rows = [["服务类型", "时限"], ["无理由退款", "7 天"], ["换货", "7 天"]]
    md = rows_to_markdown(rows)
    assert md.count("| 服务类型 | 时限 |") == 1
    assert "| 无理由退款 | 7 天 |" in md


def test_large_table_splits_and_repeats_header() -> None:
    rows = [["列名", "值"]] + [[f"k{i}", str(i)] for i in range(60)]
    chunks = split_markdown_table(rows, max_rows=20)
    assert len(chunks) == 3
    assert all(c.startswith("| 列名 | 值 |") for c in chunks)
```

**Step 2: 跑测试确认失败** → `py -3.13 -m pytest tests/test_tables.py -q`（FAIL）

**Step 3: 实现**

```python
def rows_to_markdown(rows: list[list[str]]) -> str:
    """二维数组 → Markdown 表格；首行当表头，缺列补空单元格。"""


def split_markdown_table(rows: list[list[str]], *, max_rows: int = 20) -> list[str]:
    """大表按行切块，**每块都重复表头**——否则召回单块后列语义丢失。"""


def html_table_to_rows(html: str) -> list[list[str]]:
    """unstructured 的 table 块 text 可能是 HTML；用标准库解析，不引入 bs4。"""
```

**Step 4: 跑测试** → PASS

**Step 5: Commit**

```bash
git add src/tables.py src/parsers/pdf.py tests/test_tables.py
git commit -m "feat(parser): convert tables to markdown with repeated headers"
```

---

### Task 9: P1 端到端冒烟

**Files:**
- Create: `scripts/demo_ingest.py`

**Step 1: 实现**

```python
"""上传 → worker → 检索 的最小闭环演示，退出码 0/1。"""
# 1) 调 POST /v1/kbs/{kb}/documents 上传 policy_table.pdf
# 2) 轮询 GET /v1/jobs/{id}，最多 60s
# 3) 调 POST /v1/kbs/{kb}/search 查"无理由退款多久"
# 4) 断言命中的 chunk 文本包含 "7 天"
```

**Step 2: 运行**

```bash
py -3.13 scripts/demo_ingest.py
```
Expected: `PASS upload→ingest→search`，退出码 0

**Step 3: Commit**

```bash
git add scripts/demo_ingest.py
git commit -m "test(e2e): add ingest pipeline smoke demo"
```

---

## P2：扫描件 OCR 兜底

### Task 10: OCR 接口与降级

**Files:**
- Modify: `parser/main.py`（blocks 为空 → RapidOCR）、`parser/requirements.txt`、`parser/Dockerfile`（预置模型）
- Test: `tests/test_ocr_branch.py`（mock OCR 引擎，不下载模型）

步骤：先写"解析结果为空时调用 OCR"的测试（注入 fake ocr 函数）→ 实现 `_ocr_pdf(content) -> list[Block]` → 容器内快速失败返回 422 并提示 → 用一份真实扫描件 PDF 手工验收（无扫描件则跳过并记录）。

### Task 11: 入库侧识别扫描件

**Files:**
- Modify: `src/ingest.py`
- Test: `tests/test_ingest.py` 增加用例

步骤：`ParserUnavailable("扫描件")` → 记录 `job.error` 为可读原因，不重试 3 次（避免浪费），直接 failed 并给出"需要 OCR"提示。

---

## P3：连接器

### Task 12: Source 协议与本地目录

**Files:** Create `src/connectors/__init__.py`、`src/connectors/base.py`、`src/connectors/local_dir.py`；Test `tests/test_connector_local.py`

步骤：定义 `DocumentRef(uri, mtime, size, hash)` 与 `Source.sync()`；本地目录按 mtime+hash 增量；测试"第二次同步 0 新增"。

### Task 13: 飞书连接器（可选注册）

**Files:** Create `src/connectors/feishu.py`；Test `tests/test_connector_feishu.py`（MockTransport）

步骤：凭证缺失 → `available=False` 且不注册；用 MockTransport 验证按 revision 增量；不写真实网络调用。

### Task 14: 连接器接入 worker

**Files:** Modify `scripts/run_worker.py`、`src/ingest_jobs.py`

步骤：worker 每轮先跑 `source.sync()`（间隔可配），新增文档走同一 `enqueue` → 复用入库链路。

---

## P4：trace 与运营报表

### Task 15: trace 记录

**Files:** Create `src/trace.py`；Modify `app/routers/search.py`、`app/routers/chat.py`；Test `tests/test_trace.py`

步骤：`trace_id = uuid4().hex[:12]`；记录各阶段 `ms`；JSONL 落 `data/traces/`，内存环形 1000 条；`GET /v1/traces/{id}` 先查内存再查当天文件。

### Task 16: 报表接口

**Files:** Create `app/routers/stats.py`、`src/stats.py`；Test `tests/test_stats.py`

步骤：纯函数聚合（零结果查询、拒答率、覆盖率、缺口 TopN）先写单测；接口只做参数校验与调用。

---

## P5：压测与降级演练

### Task 17: 压测脚本

**Files:** Create `scripts/loadtest.py`；输出 `eval/loadtest_report.json`

步骤：asyncio 并发（默认 20 并发 × 200 请求）打 search 与 chat/stream；统计 p50/p95/p99、错误率、rerank 降级率；报告中必须包含"降级率"，否则无法区分"优化"与"砍掉慢请求"。

### Task 18: 降级演练

**Files:** `docs/runbook.md`（新建）

步骤：停 parser → 验证本地 pypdf 生效；unset EMBED_API_KEY → 验证 mock 生效；停 DB → 验证 `/ready` 返回 503。每项记录实际输出。

---

## P6：单页 UI

### Task 19: 静态页与托管

**Files:** Create `app/static/index.html`；Modify `app/main.py`（`/ui` → StaticFiles）；Test 手工清单

功能仅 6 项：选库、上传（显示 progress）、检索列表（分数+来源）、流式问答、引用点击展开、租户/角色切换。

### Task 20: UI 验收清单

**Files:** `docs/ui-checklist.md`

步骤：按清单逐项打勾，附截图路径；不做前端自动化测试。

---

## P7：门面

### Task 21: README 与 ADR

**Files:** Modify `README.md`；Create `docs/adr/0007-async-ingest.md`、`docs/adr/0008-ocr-fallback.md`

步骤：README 补架构图、指标表（真实 bge-m3）、5 条启动命令、截图；ADR 按既有五段格式（状态/背景/决策/后果/对应代码）。

### Task 22: 最终验收

```bash
py -3.13 -m pytest -q
docker compose -f deploy/docker-compose.yml up -d
py -3.13 scripts/demo_acceptance.py     # Gate C 6/6
py -3.13 scripts/demo_ingest.py         # 入库闭环
py -3.13 scripts/loadtest.py --concurrency 20 --requests 200
```
Expected：测试全绿、Gate C 6/6、入库 PASS、压测报告生成

---

## 风险与决策点（执行时必看）

| 时机 | 决策 |
|---|---|
| Task 7 构建失败 | 立刻切 pdfplumber，不要试图修 unstructured 的系统依赖 |
| Task 12 飞书无凭据 | 只交 local_dir，飞书实现标记为"未启用"，不阻塞 P4 |
| Task 17 压测指标难看 | 如实记录，优先检查 rerank 降级率与连接池，不做"调小并发"式美化 |
| 任何 Task 超过 1 天 | 停下来汇报，不要硬推到"看起来完成" |
