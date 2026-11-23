# 工作台「存储」视图 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在工作台新增只读「存储」视图，展示当前租户已上传文档的清单与用量，并新增 `GET /v1/storage` 接口。

**Architecture:** 数据全部来自 `documents` 表（不扩展 `Storage` 协议）。上传时把 `filename`/`size` 写入 `documents.meta` 填补展示缺口；`GET /v1/storage` 按 `X-Tenant-Id` 隔离，支持 kb/status/limit 过滤；后端/桶信息由只读环境变量的 `describe_storage()` 提供。前端新建 `views/storage.mjs`，照抄 overview 的 controller 骨架与 `workbuddy:refresh` 事件刷新，在 `app.mjs`/`index.html`/i18n 接线。设计与验收口径见 `docs/superpowers/specs/2026-11-23-storage-view-design.md`。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / 原生 HTML/CSS/JS / pytest / Node test runner。

---

## 执行约定

- 所有命令在 `projects/rag-knowledge-base` 目录下执行；Python 一律 `py -3.13`。
- 每完成一个 Task：写失败测试 → 运行确认 RED → 最小实现 → 运行确认 GREEN → 只按文件名暂存并 commit。
- Python 测试：`py -3.13 -m pytest tests/test_xxx.py -q`；全量：`py -3.13 -m pytest -q`。
- Node 测试必须显式列出文件：
  `node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs`
- 提交消息风格：`feat(scope): …` / `fix(scope): …` / `test(scope): …`；禁止 `git push`。
- 不提交 `.superpowers/`、`data/`、`.env`；不修改 `eval/*`。

---

## Task 1: 上传时把 filename/size 写入 documents.meta

**Files:**
- Modify: `tests/test_api_documents.py`（追加断言 meta 的测试）
- Modify: `app/routers/documents.py:16-22, 92-109`（import json / typing.Any，INSERT 写 meta）
- Test: `tests/test_api_documents.py::test_upload_stores_filename_and_size_in_meta`

**Step 1: 追加失败测试**

在 `tests/test_api_documents.py` 文件末尾追加：

```python
def test_upload_stores_filename_and_size_in_meta(
    client: TestClient, engine, sample_pdf_bytes: bytes
) -> None:
    response = _upload(client, sample_pdf_bytes, filename="report.pdf")

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]

    with engine.connect() as connection:
        meta = connection.execute(
            text("SELECT meta FROM documents WHERE id = :id"), {"id": doc_id}
        ).scalar_one()

    assert meta["filename"] == "report.pdf"
    assert meta["size"] == len(sample_pdf_bytes)
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_api_documents.py::test_upload_stores_filename_and_size_in_meta -q
```

Expected: FAIL，`meta` 是 `{}`，`meta["filename"]` 抛 `KeyError`（或 `assert meta["filename"] == "report.pdf"` 失败）。

**Step 3: 实现**

`app/routers/documents.py` 导入区（第 16–22 行）：

Before：

```python
import hashlib
import re
from pathlib import Path
from typing import Annotated
from uuid import uuid4
```

After：

```python
import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4
```

INSERT 块（第 92–109 行）：

Before：

```python
            inserted = connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:id, :t, :kb, :title, :url, :hash, 'pending', '{}'::jsonb) "
                    "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "id": doc_id,
                    "t": tenant_id,
                    "kb": kb_id,
                    # 原始文件名不参与命名，title 先用服务端生成的 doc_id 占位
                    "title": doc_id,
                    "url": (Path(tenant_id) / kb_id / f"{doc_id}{suffix}").as_posix(),
                    "hash": content_hash,
                },
            ).scalar_one_or_none()
```

After：

```python
            # 原始文件名与大小写入 meta，供「存储」视图展示；source_url 仍是 key
            doc_meta = json.dumps(
                {"filename": file.filename or "", "size": len(content)},
                ensure_ascii=False,
            )
            inserted = connection.execute(
                text(
                    "INSERT INTO documents "
                    "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                    "VALUES (:id, :t, :kb, :title, :url, :hash, 'pending', CAST(:meta AS jsonb)) "
                    "ON CONFLICT (tenant_id, kb_id, content_hash) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "id": doc_id,
                    "t": tenant_id,
                    "kb": kb_id,
                    # 原始文件名不参与命名，title 先用服务端生成的 doc_id 占位
                    "title": doc_id,
                    "url": (Path(tenant_id) / kb_id / f"{doc_id}{suffix}").as_posix(),
                    "hash": content_hash,
                    "meta": doc_meta,
                },
            ).scalar_one_or_none()
```

> 注：`Any` 这里先导入，Task 3 的端点会用到；若 Task 1 单独 commit 时有未使用告警无所谓（ruff 未在 CI 强制运行）。

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_api_documents.py -q
```

Expected: 全部 passed（含新增用例，约 6+ passed）。

**Step 5: Commit**

```powershell
git add app/routers/documents.py tests/test_api_documents.py
git commit -m "feat(upload): persist filename and size in documents.meta"
```

---

## Task 2: describe_storage() 存储描述符

**Files:**
- Modify: `tests/test_storage.py`（追加 describe_storage 用例）
- Modify: `src/storage.py`（新增 `describe_storage` 函数）
- Test: `tests/test_storage.py` 新增的 3 个用例

**Step 1: 追加失败测试**

在 `tests/test_storage.py` 末尾追加：

```python
def test_describe_storage_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    from src import storage

    assert storage.describe_storage() == {"backend": "local", "bucket": None}


def test_describe_storage_s3_with_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "rag-kb")

    from src import storage

    assert storage.describe_storage() == {"backend": "s3", "bucket": "rag-kb"}


def test_describe_storage_unknown_backend_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "weird")

    from src import storage

    assert storage.describe_storage()["backend"] == "local"
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_storage.py -q
```

Expected: FAIL，`ImportError: cannot import name 'describe_storage'` 或 3 个 AttributeError。

**Step 3: 实现**

在 `src/storage.py` 的 `build_storage()` 函数之后追加：

```python
def describe_storage() -> dict[str, str | None]:
    """只读环境变量返回当前存储后端描述，不做任何 I/O。

    展示用，未知 backend 降级 local（与 build_storage 的严格报错不同），
    保证存储视图即使配置异常也能渲染文档列表。
    """
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    if backend not in {"local", "s3"}:
        backend = "local"
    bucket: str | None = None
    if backend == "s3":
        bucket = os.environ.get("S3_BUCKET", "").strip() or None
    return {"backend": backend, "bucket": bucket}
```

确认 `src/storage.py` 顶部已 `import os`（已有）。

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_storage.py -q
```

Expected: 全部 passed（含新增 3 个）。

**Step 5: Commit**

```powershell
git add src/storage.py tests/test_storage.py
git commit -m "feat(storage): add describe_storage descriptor"
```

---

## Task 3: GET /v1/storage 端点与响应模型

**Files:**
- Create: `tests/test_api_storage.py`
- Modify: `app/schemas.py`（追加 Storage* 模型）
- Modify: `app/routers/documents.py`（追加 GET 端点、import）
- Test: `tests/test_api_storage.py`

**Step 1: 写失败测试**

新建 `tests/test_api_storage.py`：

```python
"""存储视图接口测试：GET /v1/storage。

覆盖：响应结构、kb/status 过滤、租户隔离、非法 kb_id→400、local 后端 bucket 为 null。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_storage"
OTHER_TENANT = "t_storage_other"
KB = "kb_storage"
KB2 = "kb_storage_2"
UPLOAD_ROOT = Path("data/uploads")


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用，跳过接口集成测试：{exc}")

    _cleanup(db_engine)
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'storage')"), {"t": TENANT}
        )
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'other')"), {"t": OTHER_TENANT}
        )
        for kb in (KB, KB2):
            connection.execute(
                text(
                    "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                    "VALUES (:kb, :t, :kb, '', 'active')"
                ),
                {"kb": kb, "t": TENANT},
            )
        _insert_doc(db_engine, TENANT, KB, "doc-ready", "ready", "政策.docx", 82000)
        _insert_doc(db_engine, TENANT, KB, "doc-pending", "pending", "数据.xlsx", 1200000)
        _insert_doc(db_engine, TENANT, KB2, "doc-failed", "failed", "扫描.pdf", 3400000)
        _insert_doc(db_engine, OTHER_TENANT, "kb_x", "doc-other", "ready", "别户.md", 100)

    yield db_engine

    _cleanup(db_engine)
    db_engine.dispose()
    shutil.rmtree(UPLOAD_ROOT / TENANT, ignore_errors=True)


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        for t in (TENANT, OTHER_TENANT):
            connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM documents WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})


def _insert_doc(engine, tenant, kb, doc_id, status, filename, size) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO documents "
                "(id, tenant_id, kb_id, title, source_url, content_hash, status, meta) "
                "VALUES (:id, :t, :kb, :title, :url, :hash, :status, CAST(:meta AS jsonb))"
            ),
            {
                "id": doc_id,
                "t": tenant,
                "kb": kb,
                "title": doc_id,
                "url": f"{tenant}/{kb}/{doc_id}",
                "hash": doc_id,
                "status": status,
                "meta": json.dumps({"filename": filename, "size": size}, ensure_ascii=False),
            },
        )


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def _list(client, **params):
    return client.get("/v1/storage", params=params, headers={"X-Tenant-Id": TENANT})


def test_storage_response_shape_and_content(client: TestClient) -> None:
    resp = _list(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] in {"local", "s3"}
    assert body["bucket"] is None or isinstance(body["bucket"], str)
    assert body["stats"]["total"] == 3
    assert body["stats"]["bytes"] == 82000 + 1200000 + 3400000
    assert body["stats"]["by_status"] == {"ready": 1, "pending": 1, "failed": 1}
    filenames = {d["filename"] for d in body["documents"]}
    assert {"政策.docx", "数据.xlsx", "扫描.pdf"} <= filenames
    for d in body["documents"]:
        assert set(d) == {"id", "kb_id", "filename", "size", "status", "created_at"}


def test_storage_filter_by_kb(client: TestClient) -> None:
    resp = _list(client, kb_id=KB)
    body = resp.json()
    assert body["stats"]["total"] == 2
    assert {d["kb_id"] for d in body["documents"]} == {KB}


def test_storage_filter_by_status(client: TestClient) -> None:
    resp = _list(client, status="failed")
    body = resp.json()
    assert body["stats"]["total"] == 1
    assert body["documents"][0]["status"] == "failed"


def test_storage_tenant_isolation(client: TestClient) -> None:
    # 用别的租户头请求，看不到 TENANT 的文档
    resp = client.get("/v1/storage", headers={"X-Tenant-Id": OTHER_TENANT})
    body = resp.json()
    assert body["stats"]["total"] == 1
    assert body["documents"][0]["filename"] == "别户.md"


def test_storage_invalid_kb_id_returns_400(client: TestClient) -> None:
    resp = _list(client, kb_id="../evil")
    assert resp.status_code == 400


def test_storage_limit_caps_results(client: TestClient) -> None:
    resp = _list(client, limit=1)
    body = resp.json()
    assert len(body["documents"]) == 1
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_api_storage.py -q
```

Expected: FAIL，`404 Not Found`（端点不存在）。

**Step 3: 实现响应模型**

在 `app/schemas.py` 末尾追加：

```python
class StorageDocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    size: int | None
    status: str
    created_at: datetime


class StorageStats(BaseModel):
    total: int
    bytes: int
    by_status: dict[str, int]


class StorageResponse(BaseModel):
    backend: Literal["local", "s3"]
    bucket: str | None
    stats: StorageStats
    documents: list[StorageDocumentOut]
```

**Step 4: 实现端点**

`app/routers/documents.py`：

导入区补充（第 24–32 行附近）：

Before：

```python
from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import text

from app.deps import EngineDep, TenantId
from app.schemas import UploadDocumentResponse
from app.services import ensure_kb
from src import ingest_jobs
from src.ingest_events import record_job_event
from src.storage import StorageError, build_storage
```

After：

```python
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from sqlalchemy import text

from app.deps import EngineDep, TenantId
from app.schemas import StorageResponse, UploadDocumentResponse
from app.services import ensure_kb
from src import ingest_jobs
from src.ingest_events import record_job_event
from src.storage import StorageError, build_storage, describe_storage
```

在文件末尾（`upload_document` 之后）追加端点：

```python
def _doc_filename(meta_filename: str | None, source_url: str, doc_id: str) -> str:
    """meta.filename → source_url basename → doc_id。"""
    if meta_filename:
        return meta_filename
    base = Path(source_url).name if source_url else ""
    return base or doc_id


@router.get("/storage", response_model=StorageResponse)
def list_storage(
    tenant_id: TenantId,
    engine: EngineDep,
    kb_id: str | None = None,
    status: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> StorageResponse:
    if kb_id is not None:
        _validate_id("kb_id", kb_id)
    status_value = status if status in {"pending", "ready", "failed"} else None

    where = ["tenant_id = :t"]
    params: dict[str, Any] = {"t": tenant_id, "limit": limit}
    if kb_id:
        where.append("kb_id = :kb")
        params["kb"] = kb_id
    if status_value:
        where.append("status = :status")
        params["status"] = status_value

    sql = text(
        "SELECT id, kb_id, source_url, meta->>'filename' AS filename, "
        "NULLIF(meta->>'size', '')::bigint AS size, status, created_at "
        "FROM documents WHERE " + " AND ".join(where) + " "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    with engine.connect() as connection:
        rows = connection.execute(sql, params).mappings().all()

    documents = []
    by_status: dict[str, int] = {}
    total_bytes = 0
    for row in rows:
        filename = _doc_filename(row["filename"], row["source_url"] or "", row["id"])
        size = int(row["size"]) if row["size"] is not None else None
        documents.append(
            {
                "id": row["id"],
                "kb_id": row["kb_id"],
                "filename": filename,
                "size": size,
                "status": row["status"],
                "created_at": row["created_at"],
            }
        )
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        total_bytes += size or 0

    desc = describe_storage()
    return StorageResponse(
        backend=desc["backend"],
        bucket=desc["bucket"],
        stats={"total": len(documents), "bytes": total_bytes, "by_status": by_status},
        documents=documents,
    )
```

> 注意：`datetime` 字段直接由 pydantic 从 SQLAlchemy 的 datetime 对象序列化；`StorageResponse` 构造时传 dict 列表，pydantic 会校验。

**Step 5: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_api_storage.py -q
```

Expected: 全部 passed。

再跑一次 documents 全量确保没回归：

```powershell
py -3.13 -m pytest tests/test_api_documents.py tests/test_api_storage.py -q
```

Expected: 全绿。

**Step 6: Commit**

```powershell
git add app/schemas.py app/routers/documents.py tests/test_api_storage.py
git commit -m "feat(api): add GET /v1/storage endpoint"
```

---

## Task 4: 前端 storage 视图模块 + i18n 文案 + 单测

**Files:**
- Create: `app/static/js/views/storage.mjs`
- Create: `tests/js/storage.test.mjs`
- Modify: `app/static/js/i18n/en.mjs`（加 `nav.storage` + `storage.*`）
- Modify: `app/static/js/i18n/zh-CN.mjs`（加 `nav.storage` + `storage.*`）
- Test: `tests/js/storage.test.mjs`

**Step 1: 写失败测试**

新建 `tests/js/storage.test.mjs`（照抄 `tests/js/overview.test.mjs` 的 FakeRoot + setLocale 模式）：

```js
import test from "node:test";
import assert from "node:assert/strict";

import { setLocale } from "../../app/static/js/i18n/index.mjs";
import {
  formatBytes,
  statusLabel,
  statusClass,
  renderRows,
} from "../../app/static/js/views/storage.mjs";

test.beforeEach(() => {
  setLocale("en", { persist: false });
});

test("formatBytes handles bytes, kb, mb and missing", () => {
  assert.equal(formatBytes(0), "—");
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0 MB");
});

test("statusLabel maps known statuses and passes unknown through", () => {
  setLocale("en", { persist: false });
  assert.equal(statusLabel("ready"), "Ready");
  assert.equal(statusLabel("pending"), "Processing");
  assert.equal(statusLabel("failed"), "Failed");
  assert.equal(statusLabel("weird"), "weird");
});

test("statusClass maps ready/pending/failed, empty otherwise", () => {
  assert.equal(statusClass("ready"), "ok");
  assert.equal(statusClass("pending"), "busy");
  assert.equal(statusClass("failed"), "bad");
  assert.equal(statusClass("x"), "");
});

test("renderRows builds table rows with filename, size, status", () => {
  const html = renderRows([
    { id: "d1", kb_id: "kb1", filename: "政策.docx", size: 82000, status: "ready" },
    { id: "d2", kb_id: "kb1", filename: "坏文件.pdf", size: null, status: "failed" },
  ]);
  assert.match(html, /政策\.docx/);
  assert.match(html, /Ready/);
  assert.match(html, /Failed/);
  assert.match(html, /—/); // null size → em dash
  assert.doesNotMatch(html, /weird/);
});

test("renderRows empty list returns empty string", () => {
  assert.equal(renderRows([]), "");
});
```

**Step 2: 跑测试确认失败**

Run:

```powershell
node --test tests/js/storage.test.mjs
```

Expected: FAIL，`Cannot find module` / 导出不存在。

**Step 3: 加 i18n 文案**

`app/static/js/i18n/en.mjs`，在 `nav.audit` 之后加 `nav.storage`，并在合适位置（文件末尾 `}` 之前）加 `storage.*`：

```js
  "nav.storage": "Storage",
```

```js
  "storage.title": "Storage",
  "storage.kicker": "Storage",
  "storage.intro": "Read-only view of uploaded documents and storage usage. Uploads happen in Lab.",
  "storage.backend": "Backend",
  "storage.bucket": "Bucket",
  "storage.documents": "Documents",
  "storage.totalSize": "Total size",
  "storage.ready": "Ready",
  "storage.filterKb": "KB",
  "storage.filterStatus": "Status",
  "storage.filterAll": "All",
  "storage.colFilename": "Filename",
  "storage.colKb": "KB",
  "storage.colSize": "Size",
  "storage.colStatus": "Status",
  "storage.colCreatedAt": "Uploaded",
  "storage.status.ready": "Ready",
  "storage.status.pending": "Processing",
  "storage.status.failed": "Failed",
  "storage.empty": "No documents yet.",
  "storage.loading": "Loading…",
  "storage.readonly": "Read-only view · Upload in Lab",
```

`zh-CN.mjs` 对应中文：

```js
  "nav.storage": "存储",
```

```js
  "storage.title": "存储",
  "storage.kicker": "存储",
  "storage.intro": "已上传文档与存储用量的只读视图，上传请到实验室。",
  "storage.backend": "后端",
  "storage.bucket": "桶",
  "storage.documents": "文档",
  "storage.totalSize": "总大小",
  "storage.ready": "就绪",
  "storage.filterKb": "知识库",
  "storage.filterStatus": "状态",
  "storage.filterAll": "全部",
  "storage.colFilename": "文件名",
  "storage.colKb": "KB",
  "storage.colSize": "大小",
  "storage.colStatus": "状态",
  "storage.colCreatedAt": "上传时间",
  "storage.status.ready": "就绪",
  "storage.status.pending": "入库中",
  "storage.status.failed": "失败",
  "storage.empty": "暂无文档。",
  "storage.loading": "加载中…",
  "storage.readonly": "只读视图 · 上传请去实验室",
```

> 确认 `nav.storage` 与 `storage.*` 插入时上一行末尾有逗号，字典键值对之间用逗号分隔（与现有条目保持一致）。

**Step 4: 创建视图模块**

新建 `app/static/js/views/storage.mjs`：

```js
import { apiJson } from "../api.mjs";
import { escapeHtml } from "../core.mjs";
import { formatError, t } from "../i18n/index.mjs";

export function formatBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let size = n;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i += 1;
  }
  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function statusLabel(status) {
  switch (status) {
    case "ready": return t("storage.status.ready");
    case "pending": return t("storage.status.pending");
    case "failed": return t("storage.status.failed");
    default: return status;
  }
}

export function statusClass(status) {
  if (status === "ready") return "ok";
  if (status === "pending") return "busy";
  if (status === "failed") return "bad";
  return "";
}

function formatDateTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function renderRows(documents) {
  if (!documents || documents.length === 0) return "";
  return documents
    .map((d) => {
      const cls = statusClass(d.status);
      const pill = cls
        ? `<span class="pill ${cls}">${escapeHtml(statusLabel(d.status))}</span>`
        : escapeHtml(statusLabel(d.status));
      return [
        "<tr>",
        `<td>${escapeHtml(d.filename || d.id)}</td>`,
        `<td>${escapeHtml(d.kb_id)}</td>`,
        `<td>${formatBytes(d.size)}</td>`,
        `<td>${pill}</td>`,
        `<td>${formatDateTime(d.created_at)}</td>`,
        "</tr>",
      ].join("");
    })
    .join("");
}

export function createStorage({ root, guard, scope }) {
  let active = false;
  let destroyed = false;
  let currentScope = null;
  let loaded = false;

  const statsEl = root.querySelector("[data-storage-stats]");
  const bodyEl = root.querySelector("[data-storage-body]");
  const errorEl = root.querySelector("[data-storage-error]");
  const filterKb = root.querySelector("[data-storage-filter-kb]");
  const filterStatus = root.querySelector("[data-storage-filter-status]");

  function currentScopeKey() {
    return `${scope.tenant ?? ""}:${scope.kbId ?? ""}`;
  }

  function renderStats(payload) {
    if (!statsEl) return;
    const s = payload.stats;
    statsEl.innerHTML = [
      card(t("storage.backend"), payload.backend.toUpperCase()),
      card(t("storage.bucket"), payload.bucket || "—"),
      card(t("storage.documents"), String(s.total)),
      card(t("storage.totalSize"), formatBytes(s.bytes)),
      card(t("storage.ready"), String(s.by_status?.ready ?? 0)),
    ].join("");
  }

  function card(label, value) {
    return `<div class="wb-stat-card"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`;
  }

  function renderKbOptions(documents) {
    if (!filterKb) return;
    const current = filterKb.value;
    const kbs = [...new Set(documents.map((d) => d.kb_id))].sort();
    filterKb.innerHTML =
      `<option value="">${t("storage.filterAll")}</option>` +
      kbs.map((kb) => `<option value="${escapeHtml(kb)}">${escapeHtml(kb)}</option>`).join("");
    if ([...filterKb.options].some((o) => o.value === current)) filterKb.value = current;
  }

  async function load() {
    if (destroyed || !guard.isCurrent({ scope: currentScope })) return;
    if (!errorEl || !bodyEl) return;
    errorEl.hidden = true;
    bodyEl.innerHTML = `<tr><td colspan="5">${escapeHtml(t("storage.loading"))}</td></tr>`;

    const params = new URLSearchParams();
    if (filterKb?.value) params.set("kb_id", filterKb.value);
    if (filterStatus?.value) params.set("status", filterStatus.value);

    try {
      const payload = await apiJson(`/v1/storage?${params.toString()}`, { scope });
      if (destroyed || !guard.isCurrent({ scope: currentScope })) return;
      renderStats(payload);
      renderKbOptions(payload.documents);
      bodyEl.innerHTML =
        renderRows(payload.documents) ||
        `<tr><td colspan="5">${escapeHtml(t("storage.empty"))}</td></tr>`;
      loaded = true;
    } catch (error) {
      if (destroyed) return;
      statsEl.innerHTML = "";
      bodyEl.innerHTML = `<tr><td colspan="5">${escapeHtml(t("storage.empty"))}</td></tr>`;
      errorEl.hidden = false;
      errorEl.textContent = formatError(error);
    }
  }

  function onRefresh(event) {
    const detail = event.detail;
    if (detail?.scope) currentScope = detail.scope;
    if (detail?.view !== "storage" || !active) return;
    void load();
  }

  function onFilterChange() {
    if (active) void load();
  }

  function setActive(value) {
    const next = Boolean(value);
    if (active === next) return;
    active = next;
    if (active && !loaded) void load();
  }

  function clear() {
    loaded = false;
    currentScope = null;
    if (bodyEl) bodyEl.innerHTML = "";
    if (statsEl) statsEl.innerHTML = "";
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
    if (filterKb) filterKb.innerHTML = `<option value="">${t("storage.filterAll")}</option>`;
    if (filterStatus) {
      filterStatus.innerHTML = [
        `<option value="">${t("storage.filterAll")}</option>`,
        `<option value="ready">${t("storage.status.ready")}</option>`,
        `<option value="pending">${t("storage.status.pending")}</option>`,
        `<option value="failed">${t("storage.status.failed")}</option>`,
      ].join("");
    }
  }

  function destroy() {
    destroyed = true;
    window.removeEventListener("workbuddy:refresh", onRefresh);
    window.removeEventListener("workbuddy:scopechange", onRefresh);
    filterKb?.removeEventListener("change", onFilterChange);
    filterStatus?.removeEventListener("change", onFilterChange);
  }

  window.addEventListener("workbuddy:refresh", onRefresh);
  window.addEventListener("workbuddy:scopechange", onRefresh);
  filterKb?.addEventListener("change", onFilterChange);
  filterStatus?.addEventListener("change", onFilterChange);
  currentScope = scope;
  clear();

  return { setActive, clear, destroy, load };
}
```

> 注：`guard.isCurrent({ scope })` 的签名需与其他视图一致（`overview.mjs` 传的是带 `scope` 的对象 snapshot）。若运行时报 `guard.isCurrent` 签名不符，对齐到 `overview.mjs` 第 366 行的 `guard.isCurrent(scopeSnapshot)` 调用方式。

**Step 5: 跑测试确认通过**

Run:

```powershell
node --test tests/js/storage.test.mjs
```

Expected: 全部 pass。再跑 i18n 测试确保文案键无冲突：

```powershell
node --test tests/js/i18n.test.mjs
```

Expected: 全绿。

**Step 6: Commit**

```powershell
git add app/static/js/views/storage.mjs tests/js/storage.test.mjs app/static/js/i18n/en.mjs app/static/js/i18n/zh-CN.mjs
git commit -m "feat(ui): add storage view module and i18n"
```

---

## Task 5: 接线（导航 + app.mjs 注册 + 视图容器）

**Files:**
- Modify: `app/static/index.html`（加导航项 + `#view-storage` 容器）
- Modify: `app/static/js/app.mjs`（import / 注册 / showView / clear / destroy / init）
- Test: 全量 Node 测试 + 手动浏览器走查

**Step 1: 加导航项与视图容器（index.html）**

在左侧导航 `audit` 项之后、`</ul>` 之前加一项（序号 08；若 audit 不是最后一项，把序号顺延，保持与 `app.mjs` 中顺序无关）：

```html
<li><button class="wb-nav-link" type="button" data-nav="storage" aria-controls="view-storage"><span class="wb-nav-index">08</span><span data-i18n="nav.storage">Storage</span></button></li>
```

在 `view-audit` 的 `<section>` 之后新增：

```html
<section id="view-storage" data-view="storage" hidden aria-labelledby="view-storage-title">
  <p class="wb-section-kicker" data-i18n="storage.kicker">Storage</p>
  <h2 id="view-storage-title" class="wb-section-title" data-i18n="storage.title">Storage</h2>
  <p class="wb-section-intro" data-i18n="storage.intro">Read-only view of uploaded documents and storage usage.</p>
  <div class="wb-storage-stats" data-storage-stats></div>
  <div class="wb-storage-toolbar">
    <label><span data-i18n="storage.filterKb">KB</span>
      <select data-storage-filter-kb></select></label>
    <label><span data-i18n="storage.filterStatus">Status</span>
      <select data-storage-filter-status></select></label>
    <span class="wb-storage-readonly" data-i18n="storage.readonly">Read-only view · Upload in Lab</span>
  </div>
  <div class="wb-error" data-storage-error hidden></div>
  <table class="wb-storage-table">
    <thead><tr>
      <th data-i18n="storage.colFilename">Filename</th>
      <th data-i18n="storage.colKb">KB</th>
      <th data-i18n="storage.colSize">Size</th>
      <th data-i18n="storage.colStatus">Status</th>
      <th data-i18n="storage.colCreatedAt">Uploaded</th>
    </tr></thead>
    <tbody data-storage-body></tbody>
  </table>
</section>
```

> 样式：`wb-storage-stats`、`wb-storage-toolbar`、`wb-storage-table` 可先不写专属 CSS，复用现有 `.wb-stat-card`（Task 4 已用）与浏览器默认表格；若需美化再补 `app/static/styles.css`（不在本 Task 范围，YAGNI）。

**Step 2: 注册到 app.mjs**

按 `app.mjs` 现有模式做 6 处小改：

1. import（与其他视图 import 同区）：
   ```js
   import { createStorage } from "./views/storage.mjs";
   ```
2. `VIEW_NAMES` 数组加 `"storage"`。
3. controller 变量区加 `let storageController;`。
4. `showView` 的 `if (name === "...")` 链加：
   ```js
   if (name === "storage") storageController?.setActive(true);
   ```
   并在 deactivate 分支加 `storageController?.setActive(false);`。
5. `clearViewState` 的视图列表数组加 `"storage"`，并加 `storageController?.clear();`。
6. `destroyWorkbench` 加 `storageController?.destroy?.();`；`initializeWorkbench` 里 `controllers` 之前创建：
   ```js
   storageController = createStorage({ root, guard, scope });
   ```
   并把 `storageController.clear?.()` 纳入初始化时的清空调用（与其他视图一致）。

**Step 3: 跑全量 Node 测试**

Run:

```powershell
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs
```

Expected: 全绿（130+ 新增若干 pass）。

**Step 4: 手动走查**

若 dev server 未运行：

```powershell
$env:OPS_ENABLE_ACTIONS=1; py -3.13 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/ui#/storage`，确认：
- 左侧出现「存储」，点击进入，URL hash 变为 `#/storage`。
- 表头/卡片文案显示（中英文切换正常），无裸 key。
- 有上传文档时表格有数据；无数据时显示空态。

**Step 5: Commit**

```powershell
git add app/static/index.html app/static/js/app.mjs
git commit -m "feat(ui): wire storage view into workbench"
```

---

## Task 6: 全量回归与验收

**Files:** 无新增；仅运行验证。

**Step 1: 全量 pytest**

Run:

```powershell
py -3.13 -m pytest -q
```

Expected: 全绿（在上一轮 704 基础上 +6 个 storage 用例）。

**Step 2: 全量 Node 测试**

Run:

```powershell
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs
```

Expected: 全绿。

**Step 3: 端到端手测（local 与 s3 各一次）**

1. `STORAGE_BACKEND=local`（默认）：在实验室上传一份 `.docx`，去「存储」视图确认文件名、大小、状态（pending→刷新 ready）、时间正确；切换顶部租户看不到对方文档。
2. `STORAGE_BACKEND=s3`：`.env` 配好 MinIO（`S3_ENDPOINT=http://127.0.0.1:9000`、`S3_BUCKET=rag-kb`、密钥），重启 api + worker，上传一份，确认视图显示且 MinIO 桶里有对象。

**Step 4: 对照 spec §9 验收清单**

逐条核对 `docs/superpowers/specs/2026-11-23-storage-view-design.md` §9 的 7 条，全部通过。

**Step 5: 如有修复，单独 commit；否则汇总报告**

若无额外改动，无需 commit。向用户报告各 commit SHA 与验证结果。
