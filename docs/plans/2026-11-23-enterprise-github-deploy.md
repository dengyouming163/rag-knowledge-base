# 企业 RAG 平台工程补齐、GitHub 专业化与演示部署 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 收口未提交 WIP，补齐 CORS / 上传上限 / 向量漂移检测与重嵌 / worker 租约回收 / 演示网关，把仓库改造成 Apache-2.0 专业开源项目，并交付 `:18000` 的 Basic Auth 演示环境与双语文档。

**Architecture:** 新增能力全部由环境变量驱动（`CORS_ALLOW_ORIGINS`、`MAX_UPLOAD_BYTES`、`JOB_LEASE_SECONDS`），默认值保持现有行为。演示环境以 nginx 边车为唯一公网入口（Basic Auth + limit_req + 50m），api/db/minio/worker 只在内网；用 `deploy/docker-compose.demo.yml` 覆盖层复用现有 compose，独立 project `ragkb-demo`。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / PostgreSQL+pgvector / Alembic / 原生 JS（node:test）/ nginx / Docker Compose / GitHub Actions / ruff。

---

## 执行约定

- 工作目录 `D:\wwwroot\projectwork\projects\rag-knowledge-base`；Python 一律 `py -3.13`，pytest 用 `py -3.13 -m pytest`。
- Node 测试必须显式列文件：`node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs`
- 每个 Task：写失败测试 → 跑出 RED → 最小实现 → 跑出 GREEN → 按文件名 `git add` → commit（Conventional Commits）。
- 禁止 `git push`，唯一例外是 Task 16 且用户已提供凭据。不提交 `.env`、`data/`、`.superpowers/`、`deploy/nginx/htpasswd`。

---

## Phase 0：WIP 收口

### Task 1: 审查并按主题分 4 批提交未提交改动

**Files（现状）：** 已修改 `eval/baseline.json`、`eval/refusal_calibration.json`、`scripts/calibrate_refusal.py`、`scripts/run_eval.py`、`src/__init__.py`、`src/pipeline.py`、`src/retrieve.py`、`tests/test_api_search.py`、`tests/test_pipeline.py`；未跟踪 `docs/plans/2026-11-20-workbuddy-observability.md`、`eval/baseline_gen.json`、`eval/baseline_real_gen.json`、`eval/fixtures/`、`scripts/make_pdf_fixtures.py`、`src/parsers/__init__.py`、`src/refusal.py`、`tests/test_eval_generate.py`、`tests/test_pdf_parser.py`。

**Step 1: 审查（只读）**

```powershell
git diff -- eval/baseline.json eval/refusal_calibration.json scripts/calibrate_refusal.py scripts/run_eval.py
git diff -- src/__init__.py src/pipeline.py src/retrieve.py tests/test_api_search.py tests/test_pipeline.py
Get-Content src/refusal.py, src/parsers/__init__.py -TotalCount 40
Get-ChildItem eval/fixtures -Recurse -Name
```

判定：`src/__init__.py` 仅 `load_dotenv()`；`src/refusal.py` 是语义置信度闸门（被 `src/pipeline.py` 引用）；`src/parsers/__init__.py` 仅是包标记。若某文件混主题，用 `git add -p` 拆分。

**Step 2: 提交 A（拒答闸门 + 环境加载）**

```powershell
py -3.13 -m pytest tests/test_pipeline.py tests/test_api_search.py tests/test_refusal_calibration.py -q
git add src/__init__.py src/refusal.py src/pipeline.py src/retrieve.py scripts/calibrate_refusal.py eval/refusal_calibration.json eval/baseline.json tests/test_pipeline.py tests/test_api_search.py
git commit -m "feat(retrieval): gate answers by semantic confidence" -m "Add src/refusal.py (REFUSAL_MIN_SIMILARITY=0.56) and record vector_score in hit metadata; load .env once in src/__init__; recalibrate refusal data and baseline."
```

**Step 3: 提交 B（PDF 解析包）**

```powershell
py -3.13 -m pytest tests/test_pdf_parser.py tests/test_parsers_office.py -q
git add src/parsers/__init__.py scripts/make_pdf_fixtures.py eval/fixtures tests/test_pdf_parser.py
git commit -m "feat(parsers): add pdf fixtures and parser package marker"
```

缺失可选依赖时先 `py -3.13 -m pip install ".[test]"`。

**Step 4: 提交 C（eval generate）**

```powershell
py -3.13 -m pytest tests/test_eval_generate.py tests/test_ci_gate.py -q
git add scripts/run_eval.py tests/test_eval_generate.py eval/baseline_gen.json eval/baseline_real_gen.json
git commit -m "feat(eval): record generation metrics in eval snapshots"
```

**Step 5: 提交 D（observability 计划）**

```powershell
git diff --check
git add docs/plans/2026-11-20-workbuddy-observability.md
git commit -m "docs(plans): add workbuddy observability implementation plan"
```

**Step 6: 全量回归**

```powershell
py -3.13 -m pytest -q
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs
git status --porcelain
```

Expected: pytest/node 全绿，`git status` 无输出。

---

## Phase A：P0 工程补齐

### Task 2: 环境变量驱动的 CORS

**Files:** Create `tests/test_api_cors.py`；Modify `app/main.py:9-19`（import）、`app/main.py:34-37`（中间件）。

**Step 1: 写失败测试**

```python
"""CORS：默认关闭；白名单内放行预检。"""
from fastapi.testclient import TestClient
from app.main import create_app

ORIGIN = "https://console.example.com"


def test_cors_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    resp = TestClient(create_app()).get("/live", headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_allows_listed_origin(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", f"{ORIGIN}, https://other.example.com")
    resp = TestClient(create_app()).options(
        "/v1/storage",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN
```

**Step 2: 确认 RED**

```powershell
py -3.13 -m pytest tests/test_api_cors.py -q
```

Expected: 第 2 条 FAIL（`KeyError: 'access-control-allow-origin'`）。

**Step 3: 实现**

`app/main.py` 加 `import os`、`from fastapi.middleware.cors import CORSMiddleware`；在 `app.add_middleware(HttpMetricsMiddleware)` 之后插入：

```python
    origins = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-Tenant-Id", "X-User-Id", "Authorization"],
            max_age=600,
        )
```

**Step 4: 确认 GREEN**

```powershell
py -3.13 -m pytest tests/test_api_cors.py tests/test_api_health.py -q
```

Expected: 全绿。

**Step 5: Commit**

```powershell
git add app/main.py tests/test_api_cors.py
git commit -m "feat(api): add env-driven CORS middleware"
```

---

### Task 3: 上传上限 `MAX_UPLOAD_BYTES`（默认 50MB）

**Files:** Modify `app/routers/documents.py:16-24,37-39,69-73`；Modify `tests/test_api_documents.py:153-179`。

**Step 1: 改写/新增测试**

把 `tests/test_api_documents.py:153-179` 两条用例替换为：

```python
def test_upload_too_large_returns_413(client, monkeypatch):
    assert documents.DEFAULT_MAX_UPLOAD_BYTES == 50 * 1024 * 1024
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    assert _upload(client, b"x" * 11).status_code == 413


def test_upload_too_large_persists_nothing(client, engine, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    assert _upload(client, b"x" * 11).status_code == 413
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    assert count == 0
    assert not (UPLOAD_ROOT / TENANT / KB).exists()


def test_upload_reads_only_one_byte_past_limit(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    monkeypatch.setattr(documents, "ensure_kb", lambda *_args: None)
    upload = UploadFile(file=io.BytesIO(b"x" * 11), filename="policy.pdf")
    original_read, sizes = upload.read, []

    async def recording_read(size: int = -1) -> bytes:
        sizes.append(size)
        return await original_read(size)

    monkeypatch.setattr(upload, "read", recording_read)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(documents.upload_document(KB, TENANT, None, upload))
    assert exc_info.value.status_code == 413
    assert sizes == [11]


def test_max_upload_bytes_falls_back_on_invalid_env(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "not-a-number")
    assert documents.max_upload_bytes() == documents.DEFAULT_MAX_UPLOAD_BYTES
```

**Step 2: 确认 RED**

```powershell
py -3.13 -m pytest tests/test_api_documents.py -q
```

Expected: `AttributeError: ... has no attribute 'DEFAULT_MAX_UPLOAD_BYTES'`。

**Step 3: 实现**

`documents.py` 加 `import os`，把第 38 行常量替换为：

```python
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def max_upload_bytes() -> int:
    raw = os.environ.get("MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES
```

把读取上限块（69-73 行）改为：

```python
    limit = max_upload_bytes()
    # 只多读 1 字节即可判断超限，不把超大文件整个读入内存
    content = await file.read(limit + 1)
    if not content:
        raise HTTPException(status_code=422, detail="empty file")
    if len(content) > limit:
        raise HTTPException(status_code=413, detail="file too large")
```

**Step 4: 确认 GREEN**

```powershell
py -3.13 -m pytest tests/test_api_documents.py -q
```

Expected: 全绿（413 发生在任何写库/落盘之前）。

**Step 5: Commit**

```powershell
git add app/routers/documents.py tests/test_api_documents.py
git commit -m "feat(upload): enforce configurable MAX_UPLOAD_BYTES"
```

---

### Task 4: 向量漂移只读接口 `GET /v1/ops/storage/embedding-drift`

**Files:** Modify `app/schemas.py`（末尾）与 `app/routers/ops.py`（import 区 + 末尾）；Modify `app/main.py`（启动 WARN）；Create `tests/test_api_embedding_drift.py`。

**Step 1: 写失败测试**

```python
"""漂移接口：按 KB 返回版本分布与漂移块数，跨租户不可见、kb 非法 400。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import create_app
from src.db.base import create_db_engine

T, OTHER, KB, KB2 = "t_drift", "t_drift_other", "kb_drift", "kb_drift_2"
CONF = "BAAI/bge-m3"
VEC = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    _cleanup(db_engine)
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'drift')"), {"t": T})
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'other')"), {"t": OTHER})
        for kb in (KB, KB2):
            c.execute(text("INSERT INTO knowledge_bases (id,tenant_id,name,description,status) VALUES (:kb,:t,:kb,'','active')"), {"kb": kb, "t": T})
        c.execute(text("INSERT INTO knowledge_bases (id,tenant_id,name,description,status) VALUES ('kb_x',:t,'kb_x','','active')"), {"t": OTHER})
    for tenant, kb, cid, ver in ((T, KB, "c1", CONF), (T, KB, "c2", CONF), (T, KB, "c3", "mock-hash"),
                                 (T, KB2, "c4", CONF), (OTHER, "kb_x", "c5", "mock-hash")):
        _seed(db_engine, tenant, kb, cid, ver)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine):
    with engine.begin() as c:
        for tenant in (T, OTHER):
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            for table in ("chunks", "documents", "knowledge_bases"):
                c.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
        c.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": T, "b": OTHER})


def _seed(engine, tenant, kb, cid, version):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        c.execute(text("INSERT INTO documents (id,tenant_id,kb_id,title,source_url,content_hash,status,meta) "
                       "VALUES (:d,:t,:kb,:d,'',:d,'ready','{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                  {"d": f"doc-{cid}", "t": tenant, "kb": kb})
        c.execute(text("INSERT INTO chunks (chunk_id,tenant_id,kb_id,doc_id,content_hash,text,token_count,"
                       "embedding,embed_model_version,meta) VALUES (:id,:t,:kb,:d,:id,'x',1,"
                       "CAST(:v AS vector),:m,'{}'::jsonb)"),
                  {"id": cid, "t": tenant, "kb": kb, "d": f"doc-{cid}", "v": VEC, "m": version})


def _get(client, monkeypatch, **params):
    monkeypatch.setenv("EMBED_MODEL", CONF)
    return client.get("/v1/ops/storage/embedding-drift", params=params,
                      headers={"X-Tenant-Id": T})


def test_drift_shape(engine, monkeypatch):
    client = TestClient(create_app())
    body = _get(client, monkeypatch, kb_id=KB).json()
    assert body["configured_version"] == CONF
    assert body["total_chunks"] == 3 and body["drift_chunks"] == 1
    assert body["kbs"] == [{"kb_id": KB, "total": 3, "drift": 1,
                            "versions": {CONF: 2, "mock-hash": 1}}]


def test_drift_isolation_and_invalid_kb(engine, monkeypatch):
    client = TestClient(create_app())
    monkeypatch.setenv("EMBED_MODEL", CONF)
    body = client.get("/v1/ops/storage/embedding-drift",
                      headers={"X-Tenant-Id": OTHER}).json()
    assert body["total_chunks"] == 1 and body["drift_chunks"] == 1
    assert _get(client, monkeypatch, kb_id="../evil").status_code == 400
```

**Step 2: 确认 RED**

```powershell
py -3.13 -m pytest tests/test_api_embedding_drift.py -q
```

Expected: 2 failed（404 路由不存在）。

**Step 3: 响应模型**

`app/schemas.py` 末尾：

```python
class KbEmbeddingDrift(BaseModel):
    kb_id: str
    total: int
    drift: int
    versions: dict[str, int]


class EmbeddingDriftResponse(BaseModel):
    configured_version: str
    total_chunks: int
    drift_chunks: int
    kbs: list[KbEmbeddingDrift]
```

**Step 4: 端点**

`app/routers/ops.py`：`from typing import Literal` 改为 `Any, Literal`；新增 `import re`、`from src.db.session import tenant_scope`、`from app.schemas import EmbeddingDriftResponse, KbEmbeddingDrift` 与 `_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")`。文件末尾：

```python
@router.get("/ops/storage/embedding-drift", response_model=EmbeddingDriftResponse)
def embedding_drift(tenant_id: TenantId, engine: EngineDep, kb_id: str | None = None):
    if kb_id is not None:
        if not _SAFE_ID.fullmatch(kb_id):
            raise HTTPException(status_code=400, detail="invalid kb_id")
        ensure_kb(engine, tenant_id, kb_id)
    configured = os.environ.get("EMBED_MODEL", "").strip() or "unknown"
    where, params = [], {}
    if kb_id:
        where.append("kb_id = :kb"); params["kb"] = kb_id
    sql = text("SELECT kb_id, coalesce(embed_model_version, '') AS version, count(*) AS n "
               "FROM chunks " + ("WHERE " + " AND ".join(where) + " " if where else "")
               + "GROUP BY kb_id, coalesce(embed_model_version, '') ORDER BY kb_id")
    with tenant_scope(engine, tenant_id) as connection:
        rows = connection.execute(sql, params).mappings().all()
    grouped = {}
    for row in rows:
        item = grouped.setdefault(row["kb_id"], {"kb_id": row["kb_id"], "total": 0,
                                                 "drift": 0, "versions": {}})
        item["total"] += int(row["n"])
        item["versions"][row["version"]] = int(row["n"])
        if row["version"] != configured:
            item["drift"] += int(row["n"])
    kbs = [KbEmbeddingDrift(**item) for item in grouped.values()]
    return EmbeddingDriftResponse(configured_version=configured,
                                  total_chunks=sum(k.total for k in kbs),
                                  drift_chunks=sum(k.drift for k in kbs), kbs=kbs)
```

**Step 5: 确认 GREEN**

```powershell
py -3.13 -m pytest tests/test_api_embedding_drift.py tests/test_ops_summary_api.py -q
```

Expected: 全绿。

**Step 6: 启动时 WARN（规格 §A4，只告警不重嵌）**

`app/main.py` 加 `import logging`、`from sqlalchemy import text`、`from .deps import get_engine`、`from src.db.session import tenant_scope`，并在 `create_app()` 的 `return app` 前调用：

```python
logger = logging.getLogger(__name__)
_drift_checked = False


def _warn_embedding_drift() -> None:
    """每进程只查一次；任何失败静默，启动自检不能拖垮服务。"""
    global _drift_checked
    if _drift_checked:
        return
    _drift_checked = True
    configured = os.environ.get("EMBED_MODEL", "").strip()
    if not configured:
        return
    try:
        engine = get_engine()
        with engine.connect() as c:
            tenants = [row[0] for row in c.execute(text("SELECT id FROM tenants"))]
        drift = 0
        for tenant in tenants:
            with tenant_scope(engine, tenant) as conn:
                drift += conn.execute(
                    text("SELECT count(*) FROM chunks WHERE embed_model_version IS DISTINCT FROM :v"),
                    {"v": configured}).scalar_one()
        if drift:
            logger.warning("检测到 %s 个块与 EMBED_MODEL=%s 不一致；"
                           "运行 py -3.13 scripts/reembed_chunks.py --tenant <t> 重嵌", drift, configured)
    except Exception:  # noqa: BLE001
        logger.debug("embedding drift check skipped", exc_info=True)
```

**Step 7: 验证并 Commit**

```powershell
py -3.13 -c "from app.main import create_app; from fastapi.testclient import TestClient; print(TestClient(create_app()).get('/live').status_code)"
git add app/schemas.py app/routers/ops.py app/main.py tests/test_api_embedding_drift.py
git commit -m "feat(ops): expose embedding drift report and warn on startup"
```

Expected: 输出 `200`。

---

### Task 5: `scripts/reembed_chunks.py` 产品化

**Files:** Create `scripts/reembed_chunks.py`、`tests/test_reembed_chunks.py`。

`--only` 是源版本筛选：`mock-hash` → `IN ('mock-hash','unknown','')`；`bulk-random` → 该值；`all` → 只选未一致行。目标版本取 `embed.model`，哈希客户端回退 `mock-hash`。

**Step 1: 写失败测试**

```python
"""重嵌脚本：只动目标版本、dry-run 不写、二次运行幂等。"""
import importlib.util
from pathlib import Path
import pytest
from sqlalchemy import text
from src.db.base import create_db_engine

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reembed_chunks.py"
T, KB = "t_reembed", "kb_reembed"
VEC = "[" + ",".join(["0"] * 1024) + "]"


def _module():
    spec = importlib.util.spec_from_file_location("reembed_chunks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeEmbed:
    model = "bge-m3-test"

    def embed_texts(self, texts):
        return [[0.001] * 1024 for _ in texts]


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    _cleanup(db_engine)
    with db_engine.begin() as c:
        c.execute(text("INSERT INTO tenants (id,name) VALUES (:t,'reembed')"), {"t": T})
        c.execute(text("INSERT INTO knowledge_bases (id,tenant_id,name,description,status) VALUES (:kb,:t,:kb,'','active')"), {"kb": KB, "t": T})
    for cid, ver in (("re-1", "mock-hash"), ("re-2", "unknown"), ("re-3", "bulk-random")):
        _seed(db_engine, cid, ver)
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def _cleanup(engine):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        for table in ("chunks", "documents", "knowledge_bases"):
            c.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": T})
        c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": T})


def _seed(engine, cid, version):
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        c.execute(text("INSERT INTO documents (id,tenant_id,kb_id,title,source_url,content_hash,status,meta) "
                       "VALUES (:d,:t,:kb,:d,'',:d,'ready','{}'::jsonb) ON CONFLICT (id) DO NOTHING"),
                  {"d": f"doc-{cid}", "t": T, "kb": KB})
        c.execute(text("INSERT INTO chunks (chunk_id,tenant_id,kb_id,doc_id,content_hash,text,token_count,"
                       "embedding,embed_model_version,meta) VALUES (:id,:t,:kb,:d,:id,:txt,1,"
                       "CAST(:v AS vector),:m,'{}'::jsonb)"),
                  {"id": cid, "t": T, "kb": KB, "d": f"doc-{cid}", "txt": f"t-{cid}", "v": VEC, "m": version})


def _versions(engine):
    with engine.connect() as c:
        c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": T})
        return {row["chunk_id"]: row["embed_model_version"] for row in c.execute(
            text("SELECT chunk_id, embed_model_version FROM chunks WHERE tenant_id = :t"), {"t": T}).mappings()}


def test_reembed_only_mock_hash_and_idempotent(engine):
    module = _module()
    first = module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="mock-hash")
    assert first["updated"] == 2
    assert _versions(engine)["re-3"] == "bulk-random"
    again = module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="all")
    assert again["updated"] == 1  # 只剩 re-3
    assert module.reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB, only="all")["updated"] == 0


def test_dry_run_does_not_write(engine):
    result = _module().reembed(engine, FakeEmbed(), tenant_id=T, kb_id=KB,
                              only="mock-hash", dry_run=True)
    assert result["dry_run"] is True and result["updated"] == 0
    assert result["scanned"] == 2
    assert _versions(engine)["re-1"] == "mock-hash"
```

**Step 2: 确认 RED**

```powershell
py -3.13 -m pytest tests/test_reembed_chunks.py -q
```

Expected: 2 failed（脚本不存在，`spec.loader is None`）。

**Step 3: 实现脚本**

```python
#!/usr/bin/env python3
"""按 embedding 版本重嵌 chunks：幂等、可续跑、支持 dry-run。

    py -3.13 scripts/reembed_chunks.py --tenant demo --dry-run
    py -3.13 scripts/reembed_chunks.py --tenant demo --kb kb_after_sale --only mock-hash
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from src.db.base import create_db_engine
from src.db.session import tenant_scope

BATCH = 16
ONLY_FILTERS = {"mock-hash": ("mock-hash", "unknown", ""), "bulk-random": ("bulk-random",)}


def target_version(embed) -> str:
    return str(getattr(embed, "model", "") or "mock-hash")


def _vector(values) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def _select(connection, *, kb_id, only, target, limit):
    where = ["(embedding IS NULL OR embed_model_version <> :target)"]
    params: dict = {"target": target, "limit": limit}
    if kb_id:
        where.append("kb_id = :kb")
        params["kb"] = kb_id
    if only != "all":
        marks = ", ".join(f":v{i}" for i in range(len(ONLY_FILTERS[only])))
        where.append(f"embed_model_version IN ({marks})")
        params.update({f"v{i}": v for i, v in enumerate(ONLY_FILTERS[only])})
    sql = ("SELECT chunk_id, text FROM chunks WHERE " + " AND ".join(where)
           + " ORDER BY chunk_id LIMIT :limit")
    return connection.execute(text(sql), params).mappings().all()


def reembed(engine, embed, *, tenant_id, kb_id=None, only="all", limit=None, dry_run=False) -> dict:
    if only not in (*ONLY_FILTERS, "all"):
        raise ValueError(f"unknown --only: {only}")
    target = target_version(embed)
    if dry_run:
        with tenant_scope(engine, tenant_id) as connection:
            rows = _select(connection, kb_id=kb_id, only=only, target=target,
                           limit=limit or 1_000_000)
        return {"target_version": target, "scanned": len(rows), "updated": 0, "dry_run": True}
    scanned = 0
    with tenant_scope(engine, tenant_id, write=True) as connection:
        while limit is None or scanned < limit:
            rows = _select(connection, kb_id=kb_id, only=only, target=target,
                           limit=min(BATCH, limit - scanned) if limit else BATCH)
            if not rows:
                break
            for row, vector in zip(rows, embed.embed_texts([r["text"] for r in rows])):
                connection.execute(text(
                    "UPDATE chunks SET embedding = CAST(:e AS vector), "
                    "embed_model_version = :target, updated_at = now() "
                    "WHERE chunk_id = :id AND tenant_id = :t"),
                    {"e": _vector(vector), "target": target,
                     "id": row["chunk_id"], "t": tenant_id})
            scanned += len(rows)
    return {"target_version": target, "scanned": scanned, "updated": scanned, "dry_run": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按 embedding 版本重嵌 chunks")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--kb")
    parser.add_argument("--only", choices=("mock-hash", "bulk-random", "all"), default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from app.deps import get_embedder
    engine = create_db_engine()
    try:
        print(reembed(engine, get_embedder(), tenant_id=args.tenant, kb_id=args.kb,
                      only=args.only, limit=args.limit, dry_run=args.dry_run))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: 确认 GREEN**

```powershell
py -3.13 -m pytest tests/test_reembed_chunks.py -q
py -3.13 scripts/reembed_chunks.py --tenant demo --dry-run
```

Expected: 2 passed；脚本输出 `{'target_version': 'BAAI/bge-m3', 'scanned': 0, 'updated': 0, 'dry_run': True}`（空库时 scanned=0）。

**Step 5: Commit**

```powershell
git add scripts/reembed_chunks.py tests/test_reembed_chunks.py
git commit -m "feat(ops): add idempotent reembed_chunks script"
```

---

### Task 6: worker 租约回收

**现状判定（已读 `src/ingest_jobs.py:225-295`、`src/worker.py:36-45`）：** `claim_next` 只领 `queued`；`mark_failed` 有 `MAX_ATTEMPTS=3`；**没有任何 running 超时回收**，卡死任务会永久停在 running。因此本 Task 新增回收逻辑（不是补测试）。

**Files:** Modify `src/ingest_jobs.py`（`import os`、常量区、`claim_next` 之前）、`src/worker.py`（import + 领取前调用）、`tests/test_ingest_jobs.py`。

**Step 1: 写失败测试**

`tests/test_ingest_jobs.py` 导入区加 `reclaim_stale_running`，末尾追加：

```python
def test_fresh_running_job_is_not_reclaimed(engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="d_fresh")
    claim_next(engine)
    assert reclaim_stale_running(engine, lease_seconds=900) == 0
    assert get_job(engine, TENANT, job_id)["status"] == "running"


def test_stale_job_is_requeued_then_fails_after_max_attempts(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO documents (id,tenant_id,kb_id,title,source_url,content_hash,status,meta) "
            "VALUES ('d_lease',:t,:kb,'d_lease','','h_lease','pending','{}'::jsonb)"),
            {"t": TENANT, "kb": KB})
    job_id = enqueue(engine, TENANT, KB, doc_id="d_lease")
    for expected in (1, 2, 3):
        claimed = claim_next(engine)
        assert claimed["id"] == job_id and claimed["attempts"] == expected
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE ingest_jobs SET updated_at = now() - interval '1 hour' WHERE id = :id"),
                {"id": job_id})
        assert reclaim_stale_running(engine, lease_seconds=900) == 1
        job = get_job(engine, TENANT, job_id)
        assert job["status"] == ("queued" if expected < 3 else "failed")
    assert "lease" in job["error"]
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT status FROM documents WHERE id = 'd_lease'")).scalar_one() == "failed"
```

**Step 2: 确认 RED**

```powershell
py -3.13 -m pytest tests/test_ingest_jobs.py -q
```

Expected: `ImportError: cannot import name 'reclaim_stale_running'`。

**Step 3: 实现**

`src/ingest_jobs.py` 加 `import os`，常量区加 `DEFAULT_JOB_LEASE_SECONDS = 900` 与：

```python
def job_lease_seconds() -> int:
    raw = os.environ.get("JOB_LEASE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_JOB_LEASE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_JOB_LEASE_SECONDS
    return value if value > 0 else DEFAULT_JOB_LEASE_SECONDS
```

`claim_next` 之前插入：

```python
def reclaim_stale_running(engine: Engine, *, lease_seconds: int | None = None) -> int:
    """回收超过租约仍 running 的任务；attempts 已达 MAX_ATTEMPTS 则终态失败。"""
    lease = job_lease_seconds() if lease_seconds is None else lease_seconds
    with engine.begin() as connection:
        rows = connection.execute(text(
            "UPDATE ingest_jobs SET "
            "status = CASE WHEN attempts < :max THEN 'queued' ELSE 'failed' END, "
            "progress = CASE WHEN attempts < :max THEN 0 ELSE progress END, "
            "error = CASE WHEN attempts < :max THEN '' ELSE 'lease expired after ' || :lease || ' seconds' END, "
            "started_at = CASE WHEN attempts < :max THEN NULL ELSE started_at END, "
            "finished_at = CASE WHEN attempts < :max THEN NULL ELSE now() END, "
            "updated_at = now() "
            "WHERE status = 'running' "
            "AND updated_at < now() - make_interval(secs => :lease) "
            "RETURNING id, tenant_id, doc_id, status"),
            {"max": MAX_ATTEMPTS, "lease": lease}).mappings().all()
        for row in rows:
            if row["status"] == "failed":
                connection.execute(text(
                    "UPDATE documents SET status = 'failed', updated_at = now() "
                    "WHERE id = :d AND tenant_id = :t AND status <> 'ready'"),
                    {"d": row["doc_id"], "t": row["tenant_id"]})
    return len(rows)
```

`src/worker.py`：import 加 `reclaim_stale_running`，在 `worker_once` 的 `claim_next` 之前调用 `reclaim_stale_running(engine)`。

**Step 4: 确认 GREEN**

```powershell
py -3.13 -m pytest tests/test_ingest_jobs.py tests/test_worker_connectors.py -q
```

Expected: 全绿（每轮 worker 多一条回收 SQL，不影响既有断言）。

**Step 5: Commit**

```powershell
git add src/ingest_jobs.py src/worker.py tests/test_ingest_jobs.py
git commit -m "feat(worker): reclaim jobs stuck in running past JOB_LEASE_SECONDS"
```

---

### Task 7: 演示网关与 compose 覆盖层

**Files:** Create `deploy/nginx/demo.conf`、`deploy/nginx/.gitignore`、`deploy/docker-compose.demo.yml`。

**Step 1: 确认 RED**

```powershell
Test-Path deploy/nginx/demo.conf, deploy/docker-compose.demo.yml
```

Expected: 两个 `False`。

**Step 2: `deploy/nginx/demo.conf`**

```nginx
# 唯一公网入口：Basic Auth + 按 IP 限流 + 50m 上传上限 + 反代 api:8000
limit_req_zone $binary_remote_addr zone=demo_rl:10m rate=10r/s;

upstream rag_api { server api:8000; }

server {
    listen 80;
    server_name _;
    client_max_body_size 50m;
    gzip on;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    proxy_read_timeout 300s;   # /v1/chat 是 SSE 长连接
    proxy_buffering off;

    # 应用真实探针是 /live（/ready 含 DB）；网关统一暴露 /health 且免鉴权
    location = /health {
        auth_basic off;
        access_log off;
        proxy_pass http://rag_api/live;
    }

    # 控制台不缓存，避免浏览器拿旧 i18n/视图模块
    location /ui/ {
        auth_basic "RAG demo";
        auth_basic_user_file /etc/nginx/htpasswd;
        limit_req zone=demo_rl burst=20 nodelay;
        limit_req_status 429;
        proxy_pass http://rag_api;
        add_header Cache-Control "no-cache" always;
    }

    # /v1、/live、/ready、/docs、/openapi.json
    location / {
        auth_basic "RAG demo";
        auth_basic_user_file /etc/nginx/htpasswd;
        limit_req zone=demo_rl burst=20 nodelay;
        limit_req_status 429;
        proxy_pass http://rag_api;
    }
}
```

`deploy/nginx/.gitignore`：`htpasswd`（凭据由部署脚本生成，绝不入库）。

**Step 3: `deploy/docker-compose.demo.yml`**

```yaml
# 与 deploy/docker-compose.yml 合并使用；需要 Compose >= 2.24（ports: !reset）
name: ragkb-demo

x-log: &log
  driver: json-file
  options: {max-size: "10m", max-file: "3"}

x-app-env: &app-env
  DB_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD is required}
  DEFAULT_TENANT: demo
  EMBED_MODE: ${EMBED_MODE:-real}
  EMBED_MODEL: ${EMBED_MODEL:-BAAI/bge-m3}
  EMBED_BASE_URL: ${EMBED_BASE_URL:-}
  EMBED_API_KEY: ${EMBED_API_KEY:-}
  LLM_BASE_URL: ${LLM_BASE_URL:-}
  LLM_API_KEY: ${LLM_API_KEY:-}
  LLM_MODEL: ${LLM_MODEL:-}
  STORAGE_BACKEND: ${STORAGE_BACKEND:-s3}
  JOB_LEASE_SECONDS: ${JOB_LEASE_SECONDS:-900}

x-runtime: &runtime
  restart: unless-stopped
  logging: *log

services:
  gateway:
    image: nginx:1.27-alpine
    container_name: ragkb-demo-gateway
    ports: ["18000:80"]
    volumes:
      - ./nginx/demo.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/htpasswd:/etc/nginx/htpasswd:ro
    depends_on: {api: {condition: service_healthy}}
    <<: *runtime

  api:
    container_name: ragkb-demo-api
    ports: !reset []
    <<: *runtime
    environment:
      <<: *app-env
      MAX_UPLOAD_BYTES: ${MAX_UPLOAD_BYTES:-52428800}
      CORS_ALLOW_ORIGINS: ${CORS_ALLOW_ORIGINS:-}

  worker:
    container_name: ragkb-demo-worker
    <<: *runtime
    environment: *app-env

  db:
    container_name: ragkb-demo-db
    ports: !reset []
    <<: *runtime
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD:?DB_PASSWORD is required}

  minio:
    container_name: ragkb-demo-minio
    ports: !reset []
    <<: *runtime

  minio-init:
    container_name: ragkb-demo-minio-init
    logging: *log
```

**Step 4: 校验合并**

```powershell
if (-not (Test-Path deploy/nginx/htpasswd)) { "demo:placeholder" | Set-Content deploy/nginx/htpasswd }
$env:DB_PASSWORD = "config-check-only"
docker compose --env-file .env.example -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml config --quiet
docker compose --env-file .env.example -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml config | Select-String -Pattern "5433|8000:8000|9000:9000|9001:9001"
```

Expected: 第一条退出码 0 无输出；第二条无匹配（api/db/minio 不再发布端口），且可见 `18000:80`。报 `unknown tag !reset` 时升级 Compose（或改 `!override`）。

**Step 5: Commit**

```powershell
git add deploy/nginx/demo.conf deploy/nginx/.gitignore deploy/docker-compose.demo.yml
git commit -m "feat(deploy): add nginx demo gateway and compose overlay"
```

---

## Phase C：演示部署

### Task 8: `scripts/deploy_demo.sh`

**Files:** Create `scripts/deploy_demo.sh`。

**Step 1: 确认 RED**

```powershell
Test-Path scripts/deploy_demo.sh
```

Expected: `False`。

**Step 2: 实现脚本**

```bash
#!/usr/bin/env bash
# 演示环境一键部署：幂等可重跑。前置：Linux + Docker 26 + Compose >= 2.24 + root。
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=".env"
HTPASSWD="deploy/nginx/htpasswd"
COMPOSE=(docker compose --env-file "$ENV_FILE" -p ragkb-demo \
  -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml)
log() { printf '\n==> %s\n' "$*"; }
rand() { openssl rand -hex "${1:-16}"; }

log "前置检查"
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行"; exit 1; }
command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker compose version >/dev/null || { echo "缺少 docker compose v2"; exit 1; }
docker compose version | grep -Eq 'v2\.(2[4-9]|[3-9][0-9])' || \
  echo "警告：Compose 低于 v2.24，ports: !reset 可能不支持"
if ss -ltn 2>/dev/null | grep -q ':18000 ' && \
   ! docker ps --format '{{.Names}}' | grep -q '^ragkb-demo-'; then
  echo "18000 被其他进程占用，拒绝继续"; exit 1
fi
avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
[ "${avail_kb:-0}" -ge 5242880 ] || { echo "根分区可用空间不足 5G"; exit 1; }

if [ ! -f "$ENV_FILE" ]; then
  log "生成 $ENV_FILE"
  umask 077
  cat > "$ENV_FILE" <<EOF
DB_PASSWORD=$(rand 16)
STORAGE_BACKEND=s3
S3_BUCKET=rag-kb
S3_ACCESS_KEY=rag-app
S3_SECRET_KEY=$(rand 16)
MINIO_ROOT_USER=ragkb
MINIO_ROOT_PASSWORD=$(rand 16)
MINIO_VIEWER_USER=rag-viewer
MINIO_VIEWER_PASSWORD=$(rand 16)
EMBED_MODE=real
EMBED_MODEL=${EMBED_MODEL:-BAAI/bge-m3}
DEMO_USER=demo
DEMO_PASSWORD=$(openssl rand -base64 12 | tr -d '/+=')
EOF
fi
chmod 600 "$ENV_FILE"

# 把当前 shell 的 LLM_*/EMBED_* 补进 .env（不覆盖已有值）
for key in LLM_API_KEY LLM_BASE_URL LLM_MODEL EMBED_API_KEY EMBED_BASE_URL EMBED_MODEL; do
  value="${!key:-}"
  if [ -n "$value" ] && ! grep -q "^${key}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
done
set -a; . "./$ENV_FILE"; set +a
[ -n "${EMBED_API_KEY:-}" ] || echo "警告：EMBED_API_KEY 为空，入库会退回 mock 向量"
[ -n "${LLM_API_KEY:-}" ] || echo "警告：LLM_API_KEY 为空，问答不可用"

if [ ! -f "$HTPASSWD" ]; then
  log "生成 Basic Auth 凭据"
  umask 077
  docker run --rm httpd:2.4-alpine \
    htpasswd -nbB "${DEMO_USER:-demo}" "${DEMO_PASSWORD}" > "$HTPASSWD"
  chmod 600 "$HTPASSWD"
fi

log "构建并启动"
"${COMPOSE[@]}" up -d --build
log "等待数据库"
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T db pg_isready -U kb -d kb >/dev/null 2>&1 && break
  sleep 2
done
log "迁移"
"${COMPOSE[@]}" exec -T api alembic upgrade head
log "等待应用就绪"
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T api python -c \
    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)" \
    >/dev/null 2>&1 && break
  sleep 2
done

if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  log "放行 18000/tcp"
  firewall-cmd --permanent --add-port=18000/tcp >/dev/null
  firewall-cmd --reload >/dev/null
fi

log "健康检查"
curl -fsS http://127.0.0.1:18000/health >/dev/null && echo "/health OK"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/ui/)
[ "$code" = "401" ] && echo "/ui 未鉴权 401 OK" || { echo "/ui 期望 401，实际 $code"; exit 1; }
curl -fsS -u "${DEMO_USER}:${DEMO_PASSWORD}" -H 'X-Tenant-Id: demo' \
  http://127.0.0.1:18000/v1/storage >/dev/null && echo "鉴权访问 OK"

log "完成"
echo "入口：http://<server-ip>:18000/ui/"
echo "账号：${DEMO_USER} / ${DEMO_PASSWORD}（见 /opt/ragkb/.env，勿外传）"
```

**Step 3: 语法检查**

```powershell
& "C:\Program Files\Git\bin\bash.exe" -n scripts/deploy_demo.sh
```

Expected: 退出码 0、无输出（本机无 Git Bash 则跳过，服务器上再 `bash -n`）。

**Step 4: 服务器前置检查清单**

- `docker --version` ≥ 26；`docker compose version` ≥ v2.24。
- `systemctl is-active firewalld` = active；`ss -ltnp | grep ':18000 '` 无占用。
- `df -h /` 可用 ≥ 5G；`free -h` 可用 ≥ 1G；必要时 `docker builder prune -f`。
- 不与同机其他 compose 项目的容器名/卷冲突（演示统一 `ragkb-demo-*`）。

**Step 5: Commit**

```powershell
git add scripts/deploy_demo.sh
git commit -m "feat(deploy): add idempotent demo deploy script"
```

---

### Task 9: 执行演示部署（可勾选）

> **凭据规则：** 计划中不写服务器密码。SSH/SCP 由用户在终端交互输入 `<server-password>` 或使用本机 SSH key；不要把密码写入仓库、脚本或聊天记录。

**Files:** 无仓库文件新增（使用 Task 7/8 产出的 `deploy/*` 与 `scripts/deploy_demo.sh`）；部署事实记录到交付说明。

**Step 1: 传代码**

```powershell
# A. 服务器可访问 GitHub 且有推送凭据：
ssh root@<server-ip> "git clone <repo-url> /opt/ragkb"
# B. 否则用 bundle：
git bundle create ..\ragkb.bundle main
scp ..\ragkb.bundle root@<server-ip>:/opt/ragkb.bundle
ssh root@<server-ip> "cd /opt && rm -rf ragkb && git clone /opt/ragkb.bundle ragkb && cd ragkb && git checkout main && git log --oneline -1"
```

Expected: 服务器上 `git log` 显示本次分支最新提交。

**Step 2: 准备服务器 `.env`（真实 Key）**

```powershell
Copy-Item .env.example "$env:TEMP\ragkb-demo.env"
notepad "$env:TEMP\ragkb-demo.env"   # 填 LLM_API_KEY / EMBED_API_KEY
scp "$env:TEMP\ragkb-demo.env" root@<server-ip>:/opt/ragkb/.env
ssh root@<server-ip> "chmod 600 /opt/ragkb/.env && ls -l /opt/ragkb/.env"
```

Expected: `-rw------- root root ... /opt/ragkb/.env`。DB/MinIO/Basic Auth 密码由脚本生成。

**Step 3: 前置检查并启动**

```powershell
ssh root@<server-ip> "docker --version && docker compose version && df -h / && free -h && ss -ltn | grep ':18000 ' || echo '18000 free'"
ssh root@<server-ip> "cd /opt/ragkb && bash -n scripts/deploy_demo.sh && bash scripts/deploy_demo.sh"
```

Expected: 最后输出 `/health OK`、`/ui 未鉴权 401 OK`、`鉴权访问 OK`、`完成`。

**Step 4: 创建 demo 租户 / KB / 管理员**

```powershell
@'
INSERT INTO tenants (id, name) VALUES ('demo', '演示租户') ON CONFLICT (id) DO NOTHING;
INSERT INTO knowledge_bases (id, tenant_id, name, description, status)
VALUES ('kb_after_sale', 'demo', '售后政策', '演示知识库', 'active') ON CONFLICT (id) DO NOTHING;
INSERT INTO users (user_id, tenant_id, display_name, tags, role)
VALUES ('demo-user', 'demo', '演示用户', ARRAY['public'], 'admin')
ON CONFLICT (user_id) DO UPDATE SET role = 'admin';
'@ | ssh root@<server-ip> "cd /opt/ragkb && docker compose --env-file .env -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml exec -T db psql -U kb -d kb -v ON_ERROR_STOP=1"
```

Expected: `INSERT 0 1` / `INSERT 0 0`，无报错。

**Step 5: 预置语料（售后政策 1 篇 + 法律 docx 16 篇）**

本地准备 `demo-corpus/`（不入库），留档到服务器：

```powershell
scp -r demo-corpus root@<server-ip>:/opt/ragkb/demo-corpus
```

从本地通过 API 上传并轮询到 `succeeded`：

```powershell
$ErrorActionPreference = "Stop"
$base = "http://<server-ip>:18000"; $cred = "demo:<demo-password>"
foreach ($file in Get-ChildItem demo-corpus -File) {
    $raw = curl.exe -sS -u $cred -H "X-Tenant-Id: demo" -F "file=@$($file.FullName)" "$base/v1/kbs/kb_after_sale/documents"
    $upload = $raw | ConvertFrom-Json
    Write-Output "$($file.Name) -> doc=$($upload.doc_id) job=$($upload.job_id) dup=$($upload.duplicated)"
    while ($true) {
        $job = (curl.exe -sS -u $cred -H "X-Tenant-Id: demo" "$base/v1/jobs/$($upload.job_id)") | ConvertFrom-Json
        if ($job.status -in @("succeeded", "failed")) { break }
        Start-Sleep -Seconds 3
    }
    if ($job.status -ne "succeeded") { throw "$($file.Name) 入库失败：$($job.error)" }
    Write-Output "  ready"
}
```

Expected: 17 条全部 `succeeded`。失败时看 `docker compose ... logs worker --tail 100`（常见：embedding Key 无效、文件损坏）。

**Step 6: 验证入口与安全边界**

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://<server-ip>:18000/health                          # 200
curl.exe -s -o NUL -w "%{http_code}`n" http://<server-ip>:18000/ui/                             # 401
curl.exe -s -o NUL -w "%{http_code}`n" -u demo:<demo-password> http://<server-ip>:18000/ui/      # 200
curl.exe -s -u demo:<demo-password> -H "X-Tenant-Id: demo" http://<server-ip>:18000/v1/storage
curl.exe -s -u demo:<demo-password> -H "X-Tenant-Id: demo" "http://<server-ip>:18000/v1/ops/storage/embedding-drift?kb_id=kb_after_sale"
curl.exe -s -o NUL -w "%{http_code}`n" --max-time 3 http://<server-ip>:9000/                    # 非 200
```

Expected: `200 / 401 / 200`；storage 17 条；drift 的 `drift_chunks` 为 0；9000 不可达。

**Step 7: 记录部署事实**

演示地址 `http://<server-ip>:18000/ui/`；账号 `demo`（密码见服务器 `/opt/ragkb/.env` 的 `DEMO_PASSWORD`）；数据卷 `docker volume ls` 中的 `ragkb-demo_*`。

**Step 8: Commit（仅当本 Task 改了仓库文件）**

```powershell
git add scripts/deploy_demo.sh
git commit -m "fix(deploy): adjust demo deploy script for server environment"
```

---

## Phase B：GitHub 专业化

### Task 10: 根目录治理文件

**Files:** Create `LICENSE`、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`CHANGELOG.md`、`.editorconfig`、`.gitattributes`；Modify `.env.example`。

**Step 1: 确认 RED**

```powershell
Get-ChildItem LICENSE, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, CHANGELOG.md, .editorconfig, .gitattributes -ErrorAction SilentlyContinue
```

Expected: 无输出。

**Step 2: 下载标准文本**

```powershell
Invoke-WebRequest https://www.apache.org/licenses/LICENSE-2.0.txt -OutFile LICENSE
Invoke-WebRequest https://www.contributor-covenant.org/version/2/1/code_of_conduct/code_of_conduct.md -OutFile CODE_OF_CONDUCT.md
Get-Content LICENSE -TotalCount 2; Get-Content CODE_OF_CONDUCT.md -TotalCount 1
```

Expected: `Apache License` / `# Contributor Covenant Code of Conduct`。离线环境手工复制全文，不要只写一行 SPDX。

**Step 3: 其余文件**

- `SECURITY.md`：Supported Versions（仅 `main`）、Reporting（**不要**开公开 issue，邮件 `<maintainer-email>`，3 个工作日响应 / 30 天修复）、部署说明（Basic Auth、限流、50MB、无 TLS、RLS `0007`、`.env` 600）。
- `CONTRIBUTING.md`：Python 3.13 + `pip install -e ".[test,dev]"`；`py -3.13 -m pytest -q`；Node 显式文件命令；`ruff check .`；Conventional Commits；PR 附测试；不提交 `.env`/`data/`。
- `CHANGELOG.md`（Keep a Changelog + SemVer）：`[Unreleased]` 下列 Added（CORS、上传上限、drift 接口、reembed 脚本、租约回收、demo gateway、CI、ruff、双语文档）/ Changed（语义置信度拒答）；`[0.1.0] - 2026-11-23` 记录现有 RAG 闭环与驾驶舱。

```ini
# .editorconfig
root = true
[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 4
[*.{js,mjs,json,yml,yaml,md,html,css}]
indent_size = 2
[Makefile]
indent_style = tab
```

```gitattributes
* text=auto eol=lf
*.png binary
*.jpg binary
*.pdf binary
*.docx binary
*.pptx binary
*.xlsx binary
```

`.env.example` 追加：

```dotenv
# CORS 白名单（逗号分隔；留空=不启用）
CORS_ALLOW_ORIGINS=
# 单文件上传上限（字节）；nginx client_max_body_size 需同步
MAX_UPLOAD_BYTES=52428800
# running 超过该秒数视为卡死并回收
JOB_LEASE_SECONDS=900
```

**Step 4: 校验并 Commit**

```powershell
git diff --check
Select-String -Path .env.example -Pattern "CORS_ALLOW_ORIGINS|MAX_UPLOAD_BYTES|JOB_LEASE_SECONDS"
git add LICENSE SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md CHANGELOG.md .editorconfig .gitattributes .env.example
git commit -m "docs: add license, security, contributing and changelog"
```

Expected: `git diff --check` 无输出；3 个变量命中。

---

### Task 11: ruff 配置与存量修复

**Files:** Modify `pyproject.toml`。

**Step 1: 确认 RED**

```powershell
py -3.13 -m ruff check . 2>&1 | Select-Object -Last 5
```

Expected: 未安装则 `No module named ruff`；已安装则用默认规则，与目标不一致。

**Step 2: 配置**

`[project.optional-dependencies]` 追加：

```toml
dev = ["ruff>=0.5", "pytest-cov>=5"]
```

文件末尾追加：

```toml
[tool.ruff]
line-length = 100
target-version = "py311"
exclude = [".venv", "data", "eval/fixtures", "deploy"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
ignore = ["E203"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["B011"]
```

**Step 3: 安装并修复**

```powershell
py -3.13 -m pip install -e ".[dev]"
py -3.13 -m ruff check . --statistics
py -3.13 -m ruff check . --fix
py -3.13 -m ruff check .
```

Expected: 首次输出违规计数（以 `I001`、`UP` 语法升级与少量 `E501` 为主）；`--fix` 后目标 `All checks passed!`。剩余手工修，不用 `# noqa` 批量压制。

**Step 4: 回归并 Commit**

```powershell
py -3.13 -m pytest -q
git add pyproject.toml
git add -u
git commit -m "chore(lint): configure ruff and fix existing violations"
```

Expected: pytest 全绿；`git status --porcelain` 复核只含 Python 源文件。

---

### Task 12: CI、模板与 Dependabot

**Files:** Create `.github/workflows/ci.yml`、`.github/ISSUE_TEMPLATE/bug_report.yml`、`.github/ISSUE_TEMPLATE/feature_request.yml`、`.github/PULL_REQUEST_TEMPLATE.md`、`.github/dependabot.yml`。

**Step 1: 确认 RED**

```powershell
Test-Path .github/workflows/ci.yml, .github/dependabot.yml, .github/PULL_REQUEST_TEMPLATE.md
```

Expected: 全部 `False`。

**Step 2: `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push: {branches: [main]}
  pull_request:
permissions: {contents: read}
concurrency: {group: "ci-${{ github.ref }}", cancel-in-progress: true}

jobs:
  python:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env: {POSTGRES_USER: kb, POSTGRES_PASSWORD: kb, POSTGRES_DB: kb}
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U kb -d kb"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 20
    env:
      DB_HOST: 127.0.0.1
      DB_PORT: "5432"
      DB_USER: kb
      DB_PASSWORD: kb
      DB_NAME: kb
      EMBED_MODE: mock
      REFUSAL_MIN_SIMILARITY: "0"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13", cache: pip}
      - run: pip install -e ".[test,dev]"
      - run: alembic upgrade head
      - run: ruff check .
      - run: pytest -q --cov=app --cov=src --cov-report=term-missing

  frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: "20"}
      - run: >-
          node --test tests/js/core.test.mjs tests/js/overview.test.mjs
          tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs
          tests/js/storage.test.mjs

  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13", cache: pip}
      - run: pip install -e .
      - run: python scripts/run_eval.py --mock --compare eval/baseline.json
```

> `python` job 必须用 pgvector service 并先迁移：迁移里有 `vector` 扩展、HNSW 与 RLS，缺库时集成测试只会 skip。

**Step 3: 模板**

`.github/ISSUE_TEMPLATE/bug_report.yml`：

```yaml
name: Bug report
description: Report a reproducible defect
labels: ["bug"]
body:
  - type: textarea
    id: what
    attributes: {label: What happened?}
    validations: {required: true}
  - type: textarea
    id: steps
    attributes:
      label: Reproduction steps
      placeholder: |
        1. docker compose -f deploy/docker-compose.yml up -d --build
        2. POST /v1/kbs/kb_after_sale/documents ...
    validations: {required: true}
  - type: input
    id: version
    attributes: {label: Commit, placeholder: "git rev-parse --short HEAD"}
    validations: {required: true}
  - type: textarea
    id: logs
    attributes: {label: Logs, render: shell}
```

`.github/ISSUE_TEMPLATE/feature_request.yml`：

```yaml
name: Feature request
description: Propose a capability or improvement
labels: ["enhancement"]
body:
  - type: textarea
    id: problem
    attributes: {label: Problem}
    validations: {required: true}
  - type: textarea
    id: proposal
    attributes: {label: Proposal}
    validations: {required: true}
  - type: checkboxes
    id: scope
    attributes:
      label: Scope check
      options:
        - label: Not a security issue (see SECURITY.md)
          required: true
```

`.github/PULL_REQUEST_TEMPLATE.md`：

```markdown
## What changed
## Why
## How tested
- [ ] `py -3.13 -m pytest -q`
- [ ] `node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs`
- [ ] `py -3.13 -m ruff check .`
## Checklist
- [ ] No secrets / `.env` / `data/` in diff
- [ ] Docs updated when behavior changed
```

`.github/dependabot.yml`：

```yaml
version: 2
updates:
  - package-ecosystem: pip
    directory: "/"
    schedule: {interval: weekly}
  - package-ecosystem: github-actions
    directory: "/"
    schedule: {interval: monthly}
```

**Step 4: 静态校验并 Commit**

```powershell
Select-String -Path .github/workflows/ci.yml -Pattern "^  (python|frontend|eval):"
git add .github
git commit -m "ci: add python, frontend and eval workflows with templates"
```

Expected: 3 行匹配；CI 真正变绿在 Task 16（需 push）。

---

## Phase D：文档

### Task 13: 双语 README

**Files:** Modify `README.md`（英文重写）；Create `README.zh-CN.md`。

**Step 1: 确认 RED**

```powershell
Test-Path README.zh-CN.md; Select-String -Path README.md -Pattern "Live Demo"
```

Expected: `False`；无 `Live Demo`。

**Step 2: `README.md` 必写内容**

- 顶部 badges：CI（`.github/workflows/ci.yml`）、Python 3.13、Apache-2.0；语言切换 `English | [简体中文](README.zh-CN.md)`。
- What this is：多租户 RAG 知识库服务（异步入库 → 混合检索 → 引用校验生成 → 运营控制台），面向客服/合规/内部知识。
- Capabilities：异步入库（OCR 降级、父子分块、content_hash 去重）、pgvector HNSW + BM25 + weighted RRF、拒答（`REFUSAL_MIN_SIMILARITY` 0.56）、引用校验（`src/citation.py`）、SSE、RLS+ACL、ops cockpit、`GET /v1/storage`。
- Architecture：Mermaid 两段式（ingest/query），节点标注 `app/main.py`、`src/ingest.py`、`src/retrieve.py`、`src/pipeline.py`、`migrations/versions/0007_rls_chunks.py`。
- Quickstart：
  ```bash
  cp .env.example .env
  docker compose -f deploy/docker-compose.yml up -d --build
  docker compose -f deploy/docker-compose.yml exec -T api alembic upgrade head
  # http://127.0.0.1:8000/ui/
  ```
- Live Demo：`http://<demo-url>:18000/ui/`（Task 15 回填），注明 Basic Auth 与演示数据非敏感。
- API summary（指向 `docs/API.md` 与 `/docs`）：`POST /v1/kbs/{kb_id}/documents`、`GET /v1/jobs/{job_id}`、`POST /v1/kbs/{kb_id}/search`、`POST /v1/chat`(SSE)、`GET /v1/storage`、`GET /v1/ops/storage/embedding-drift`、`GET /live`、`GET /ready`。
- Evaluation（`eval/baseline_real_gen.json` 实测）：Recall@5 **1.0000**、MRR **0.9737**、Citation **1.0000**、Refusal **1.0000**、Coverage 0.9211；条件 `mode=hybrid, top_k=5, bge-m3`，语料 `eval/demo_corpus.json`，用例 `eval/cases.jsonl`；mock 基线（`eval/baseline.json`）Recall@5 0.9342 / MRR 0.8575。
- Repo layout、Roadmap（指向 `docs/ROADMAP.md`）、Security（`SECURITY.md`）、License Apache-2.0。

**Step 3: `README.zh-CN.md`**

与英文逐段对应（同标题、同命令、同指标），只翻译叙述；代码块、端点、环境变量、数字保持不变；顶部 `[English](README.md) | 简体中文`。

**Step 4: 校验并 Commit**

```powershell
Select-String -Path README.md, README.zh-CN.md -Pattern "1.0000|0.9737|embedding-drift|Live Demo"
git add README.md README.zh-CN.md
git commit -m "docs(readme): rewrite README in English and add zh-CN mirror"
```

Expected: 两份均命中指标与关键端点。

---

### Task 14: `docs/` 八篇

**Files:** Create `docs/ARCHITECTURE.md`、`DEPLOYMENT.md`、`OPERATIONS.md`、`SECURITY.md`、`EVALUATION.md`、`API.md`、`ROADMAP.md`、`README.md`。

**Step 1: 确认 RED**

```powershell
Get-ChildItem docs\ARCHITECTURE.md, docs\DEPLOYMENT.md, docs\OPERATIONS.md, docs\SECURITY.md, docs\EVALUATION.md, docs\API.md, docs\ROADMAP.md, docs\README.md -ErrorAction SilentlyContinue
```

Expected: 无输出。

**Step 2: 各篇必须写清楚的事实**

- `ARCHITECTURE.md`：分层（`app/routers` → `app/services.py` → `src/*` → pgvector/S3）；入库状态机（`queued→running→succeeded/failed`，`MAX_ATTEMPTS=3`、`JOB_LEASE_SECONDS=900`）；查询链路（改写 → 向量+BM25 → RRF → `src/refusal.py` 拒答 → 父块扩展/rerank → 生成+引用）；对象 key `{tenant_id}/{kb_id}/{doc_id}{suffix}`（`src/storage.py`）；RLS `0007`、ACL `src/acl.py`；端口 8000/5433/9000/9001/8100/18000。
- `DEPLOYMENT.md`：本地 compose 命令与迁移；环境变量表（`DB_*`、`S3_*`、`EMBED_*`、`LLM_*`、`PARSER_BASE_URL`、`CORS_ALLOW_ORIGINS`、`MAX_UPLOAD_BYTES`、`JOB_LEASE_SECONDS`、`OPS_ENABLE_ACTIONS`）；演示部署 `scripts/deploy_demo.sh` + demo overlay（Compose ≥2.24）；`alembic upgrade head` / `downgrade -1`；备份 `pg_dump` 与回滚（镜像 tag + `ragkb-demo_pgdata/uploads/miniodata`）；限制（HTTP、`kb_app` 默认密码仅内网可达）。
- `OPERATIONS.md`：`/live` vs `/ready`（缓存 5s）与 `/health`；任务排查（`/v1/ops/jobs`、`/v1/ops/jobs/{id}/events`）；人工重试需 `OPS_ENABLE_ACTIONS=1` 且 `users.role='admin'`；卡死任务回收与 SQL 核对；漂移检查与 `reembed_chunks.py --dry-run`；常见故障（embedding Key 失效、文件缺失、parser down 降级 pypdf、RLS 0 行）；日志 `/logs --tail 100`（10m×3）；压测 `scripts/loadtest.py`。
- `SECURITY.md`：信任边界（公网→gateway→api）；Basic Auth/限流/上传上限；RLS 与 `kb_app`；ACL 求交；注入防护；密钥管理；演示环境说明；上报流程指向根 `SECURITY.md`。
- `EVALUATION.md`：数据集、命令（`--mock`/`--real`/`--generate`）、指标定义、三份基线对比表（数字同 Task 13）、门禁（`scripts/ci_gate.py`、`run_eval --compare`）、局限（50 条、单领域、mock 不可用于质量结论）。
- `API.md`：端点总表 + `/docs` 为权威契约；租户头现状；错误码 400/404/413/422/429/500。
- `ROADMAP.md`：P1（JWT/OIDC、RBAC、HTTPS、Prometheus、评测扩充、多 worker/BM25 分布式）、P2（连接器、增量索引、成本看板），每条给验收口径，不写日期承诺。
- `docs/README.md`：索引表（文档/读者/一句话），链接 7 篇 + 根 `README.md`、`CHANGELOG.md`、`SECURITY.md`、`docs/adr/`、`docs/runbook.md`。

**Step 3: 校验并 Commit**

```powershell
Get-ChildItem docs\*.md | Select-Object Name, Length
Select-String -Path docs\ARCHITECTURE.md -Pattern "0007_rls_chunks|18000|HNSW"
Select-String -Path docs\OPERATIONS.md -Pattern "JOB_LEASE_SECONDS|embedding-drift|OPS_ENABLE_ACTIONS"
Select-String -Path docs\EVALUATION.md -Pattern "0.9737|1.0000"
git add docs/ARCHITECTURE.md docs/DEPLOYMENT.md docs/OPERATIONS.md docs/SECURITY.md docs/EVALUATION.md docs/API.md docs/ROADMAP.md docs/README.md
git commit -m "docs: add architecture, deployment, operations and evaluation guides"
```

Expected: 8 篇非空，路径/端口/指标全部命中；引用的文件必须真实存在。

---

### Task 15: 截图与 `<demo-url>` 回填

**Files:** Create `docs/assets/screenshots/overview.png`、`lab-qa.png`、`storage.png`；Modify `README.md`、`README.zh-CN.md`、`docs/DEPLOYMENT.md`、`docs/OPERATIONS.md`。

**Step 1: 确认 RED**

```powershell
Test-Path docs/assets/screenshots; Select-String -Path README.md, README.zh-CN.md -Pattern "demo-url"
```

Expected: `False`；两份 README 仍含占位。

**Step 2: 抓 3 张截图**

用 Playwright（`http_credentials={username:"demo", password:"<demo-password>"}`）或人工登录 `http://<server-ip>:18000/ui/`：

1. `#/overview` 等统计卡片出数 → `overview.png`。
2. `#/lab` 提「退款多久到账」，等回答+引用渲染 → `lab-qa.png`。
3. `#/storage` 确认 17 条文档 → `storage.png`。

Basic Auth 弹窗无法自动化时，用 `browser_waiting_for_user_interaction` 让用户手工登录。

**Step 3: 回填**

两份 README 的 Live Demo 段固定为：

```markdown
## Live Demo
`http://<server-ip>:18000/ui/` — HTTP Basic Auth; credentials are held by the deployment owner.

![Overview](docs/assets/screenshots/overview.png)
```

把 `lab-qa.png`、`storage.png` 嵌入 Query Lab / Storage 章节（中文版用中文 alt，路径不变）。`docs/DEPLOYMENT.md`、`docs/OPERATIONS.md` 写入真实地址（仅 IP:端口，不含凭据）。

**Step 4: 校验并 Commit**

```powershell
Get-ChildItem docs/assets/screenshots | Select-Object Name, Length
Select-String -Path README.md, README.zh-CN.md -Pattern "docs/assets/screenshots/(overview|lab-qa|storage).png"
Select-String -Path README.md, README.zh-CN.md, docs/*.md -Pattern "<demo-url>" -SimpleMatch
git add docs/assets/screenshots README.md README.zh-CN.md docs/DEPLOYMENT.md docs/OPERATIONS.md
git commit -m "docs: add demo screenshots and fill live demo URL"
```

Expected: 3 张 PNG > 20 KB；两份 README 各命中 3 个路径；最后一条无匹配。

---

## Phase E：收尾

### Task 16: 最终验收（规格 §10 的 8 条）

**Files:** 无新增；仅运行验证，必要时修复后单独提交。

**Step 1: 代码与测试（验收 1）**

```powershell
git status --porcelain
py -3.13 -m pytest -q
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs
py -3.13 -m ruff check .
```

Expected: `git status` 无输出；pytest/node 全绿；`All checks passed!`。

**Step 2: 演示入口与安全（验收 3）**

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://<server-ip>:18000/health                       # 200
curl.exe -s -o NUL -w "%{http_code}`n" http://<server-ip>:18000/ui/                          # 401
curl.exe -s -o NUL -w "%{http_code}`n" -u demo:<demo-password> http://<server-ip>:18000/ui/   # 200
curl.exe -s -o NUL -w "%{http_code}`n" --max-time 3 http://<server-ip>:5433/                 # 非 200
curl.exe -s -o NUL -w "%{http_code}`n" --max-time 3 http://<server-ip>:9000/                 # 非 200
```

Expected: 200 / 401 / 200 / 连接失败 / 连接失败。

**Step 3: UI 全流程（验收 4、5）**

`#/storage` 17 条；`#/lab` 回答带 `[chunk_id]` 引用（SSE 流式）；上传一份 `.docx` 后 `#/ingestion` 走完 `pending → running → succeeded`，`#/storage` 出现新文件名。

**Step 4: 漂移与重嵌（验收 6）**

```powershell
curl.exe -s -u demo:<demo-password> -H "X-Tenant-Id: demo" "http://<server-ip>:18000/v1/ops/storage/embedding-drift?kb_id=kb_after_sale"
ssh root@<server-ip> "cd /opt/ragkb && docker compose --env-file .env -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml exec -T api python scripts/reembed_chunks.py --tenant demo --kb kb_after_sale --dry-run"
```

Expected: `drift_chunks` 为 0；dry-run `updated: 0`，重复执行结果一致。

**Step 5: 文档与治理（验收 7、8）**

```powershell
Select-String -Path README.md, README.zh-CN.md -Pattern "docs/assets/screenshots"
Get-ChildItem LICENSE, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, CHANGELOG.md, .github/workflows/ci.yml, .github/dependabot.yml
```

Expected: 双语 README 含 3 张截图且 Live Demo 可点；治理文件与 CI 齐全。

**Step 6: CI 绿（验收 2，需推送凭据）**

```powershell
git push origin main
gh run list --limit 3
gh run watch
```

Expected: python / frontend / eval 三 job 全绿。未获凭据前**不要**推送；无 `gh` 则在 Actions 页面人工确认。

**Step 7: 部署后安全建议（交付说明，不自动执行）**

轮换 root 密码并改用 SSH key；离线备份 `/opt/ragkb/.env` 与 `pg_dump` 产物；需要 HTTPS 时在网关前置 TLS 并同步更新 demo 地址。

---

## 风险提示

1. **Task 7**：`ports: !reset []` 依赖 Compose ≥ 2.24，版本不足时 `config` 直接报错，先验 `docker compose version`。
2. **Task 9**：真实 embedding/LLM 依赖外部 API，限流/欠费/超时会表现为任务 `failed`，先上传 1 篇再全量。
3. **Task 1**：`scripts/run_eval.py` 与 `eval/*.json` diff 较大，分批暂存容易漏带/多带，每个提交后 `git show --stat` 复核。
