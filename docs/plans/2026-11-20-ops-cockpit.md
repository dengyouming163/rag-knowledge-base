# 调测驾驶舱与权限可视化 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 `/ui` 中交付微信桌面版三栏调测驾驶舱，实时展示任务、Trace、AI 指标、评测/压测、身份与权限，并支持管理员受控触发与审计。

**Architecture:** 新增 `ops_runs`/`ops_audit`/`ingest_job_events`/`ops_workers` 四张表；只读接口直接聚合现有 jobs/traces，新增独立 `ops-runner` 以白名单方式执行评测、压测和安全演练；前端保持单文件原生 HTML/CSS/JS，用左导航 + 中栏列表 + 右栏内容区承载四个模块。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / Alembic / Postgres 16 + pgvector / pytest / 原生 HTML/CSS/SVG。

**Spec:** `docs/superpowers/specs/2026-11-20-ops-cockpit-design.md`

**执行约定：**

- 所有命令在 `projects/rag-knowledge-base` 下执行，使用 `py -3.13`。
- 每完成一个 Task：先写失败测试 → 验证 RED → 最小实现 → 验证 GREEN → 全量 `py -3.13 -m pytest -q` → 只按文件名暂存并 commit。
- 表结构只通过 Alembic；禁止手写生产 DDL。
- 所有查询必须带 `tenant_id`；body 不接受 `tenant_id`；跨租户资源返回 404。
- 不修改、不暂存、不提交 spec 11.1 列出的他人 dirty/untracked 文件。
- 不覆盖 `eval/baseline*.json`；评测/压测产物只写 `data/ops/`。
- 每个 Task 完成后按中文五段格式汇报：定位、目的、做了什么、完成了什么、术语。

---

## P1：只读驾驶舱

### Task 1: 迁移 0010 与 ORM 模型

**Files:**
- Create: `migrations/versions/0010_ops_cockpit.py`
- Modify: `src/db/models.py`
- Test: `tests/test_ops_migration.py`

**Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from src.db.base import create_db_engine


@pytest.fixture(scope="module")
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    yield db_engine
    db_engine.dispose()


def test_ops_tables_exist(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {"ops_runs", "ops_audit", "ingest_job_events", "ops_workers"} <= tables


def test_ingest_jobs_has_started_at(engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("ingest_jobs")}
    assert "started_at" in columns


def test_ops_runs_has_active_unique_index(engine) -> None:
    indexes = inspect(engine).get_indexes("ops_runs")
    active = [item for item in indexes if item["name"] == "uq_ops_runs_active_kind"]
    assert active and active[0]["unique"] is True


def test_ops_workers_has_no_tenant_id(engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("ops_workers")}
    assert "worker_id" in columns
    assert "tenant_id" not in columns
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_migration.py -q`

Expected: FAIL（`ops_runs` 等表不存在）。

**Step 3: 写迁移**

`migrations/versions/0010_ops_cockpit.py`：

```python
"""调测驾驶舱：运行记录、审计、任务事件与 worker 心跳。

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_runs (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            kb_id text NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
            kind text NOT NULL CHECK (kind IN (
                'eval_mock', 'loadtest', 'drill_parser', 'drill_llm', 'drill_queue'
            )),
            status text NOT NULL DEFAULT 'queued' CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed'
            )),
            params jsonb NOT NULL DEFAULT '{}'::jsonb,
            requested_by text NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            exit_code integer,
            summary jsonb,
            artifact_path text,
            output_tail text,
            error text,
            attempts integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ops_runs_active_kind "
        "ON ops_runs (tenant_id, kb_id, kind) "
        "WHERE status IN ('queued', 'running')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_runs_tenant_kb_id "
        "ON ops_runs (tenant_id, kb_id, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_audit (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL,
            actor text NOT NULL,
            action text NOT NULL,
            target text,
            result text NOT NULL CHECK (result IN (
                'allowed', 'denied', 'succeeded', 'failed'
            )),
            reason text,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_audit_tenant_id "
        "ON ops_audit (tenant_id, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_job_events (
            id bigserial PRIMARY KEY,
            job_id bigint NOT NULL REFERENCES ingest_jobs(id) ON DELETE CASCADE,
            tenant_id text NOT NULL,
            stage text NOT NULL CHECK (stage IN (
                'queued', 'claimed', 'parsing', 'chunking',
                'embedding', 'publishing', 'succeeded', 'failed'
            )),
            status text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
            progress integer,
            message text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ingest_job_events_job_id "
        "ON ingest_job_events (job_id, id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_workers (
            worker_id text PRIMARY KEY,
            last_seen timestamptz NOT NULL,
            version text NOT NULL DEFAULT '',
            current_job_id bigint,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS started_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE ingest_jobs DROP COLUMN IF EXISTS started_at")
    op.execute("DROP TABLE IF EXISTS ops_workers")
    op.execute("DROP TABLE IF EXISTS ingest_job_events")
    op.execute("DROP TABLE IF EXISTS ops_audit")
    op.execute("DROP TABLE IF EXISTS ops_runs")
```

**Step 4: 加 ORM 模型**

在 `src/db/models.py` 追加。`BigInteger`、`Index`、`Integer`、`Text`、`String`、`func` 已导入，只需补 `JSONB`：

```python
from sqlalchemy.dialects.postgresql import JSONB
```

下面四个模型追加到文件末尾：

```python
class OpsRun(Base):
    __tablename__ = "ops_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    output_tail: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_ops_runs_tenant_kb_id", "tenant_id", "kb_id", "id"),
    )


class OpsAudit(Base):
    __tablename__ = "ops_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(128))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_ops_audit_tenant_id", "tenant_id", "id"),
    )


class IngestJobEvent(Base):
    __tablename__ = "ingest_job_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("ingest_jobs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_ingest_job_events_job_id", "job_id", "id"),
    )


class OpsWorker(Base):
    __tablename__ = "ops_workers"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    current_job_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

**Step 5: 应用迁移并验证**

Run:

```bash
py -3.13 -m alembic upgrade head
py -3.13 -m pytest tests/test_ops_migration.py tests/test_db_models.py -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add migrations/versions/0010_ops_cockpit.py src/db/models.py tests/test_ops_migration.py
git commit -m "feat(ops): add cockpit tables via migration 0010"
```

---

### Task 2: 任务阶段事件与 worker 心跳

**Files:**
- Create: `src/ingest_events.py`
- Create: `tests/test_ingest_events.py`
- Modify: `src/ingest_jobs.py`（`_JOB_COLUMNS`、`claim_next`、新增 `retry_job`）
- Modify: `src/ingest.py`（`on_stage` 回调）
- Modify: `src/worker.py`（阶段事件、失败/成功事件、心跳）
- Modify: `scripts/run_worker.py`（循环心跳）
- Modify: `app/routers/documents.py`（入队后写 queued 事件）

**Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db.base import create_db_engine
from src.ingest_events import list_job_events, record_job_event
from src.ingest_jobs import enqueue


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES ('t_events', 'events') "
                 "ON CONFLICT (id) DO NOTHING")
        )
        connection.execute(
            text("INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                 "VALUES ('kb_events', 't_events', 'events', '', 'active') "
                 "ON CONFLICT (id) DO NOTHING")
        )
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM ingest_job_events WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM ingest_jobs WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = 't_events'"))
        connection.execute(text("DELETE FROM tenants WHERE id = 't_events'"))
    db_engine.dispose()


def test_events_are_ordered_and_scoped(engine) -> None:
    job_id = enqueue(engine, "t_events", "kb_events", doc_id="doc_events")

    record_job_event(
        engine, job_id=job_id, tenant_id="t_events",
        stage="parsing", status="started", progress=30,
    )
    record_job_event(
        engine, job_id=job_id, tenant_id="t_events",
        stage="succeeded", status="succeeded", progress=100,
    )

    events = list_job_events(engine, "t_events", job_id)
    assert [event["stage"] for event in events] == ["parsing", "succeeded"]
    assert list_job_events(engine, "t_other", job_id) == []


def test_event_write_failure_does_not_raise(engine, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(engine, "begin", boom)
    record_job_event(
        engine, job_id=1, tenant_id="t_events",
        stage="failed", status="failed", progress=0, message="boom",
    )
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ingest_events.py -q`

Expected: FAIL（模块不存在）。

**Step 3: 实现事件模块**

`src/ingest_events.py`：

```python
"""任务阶段事件与 worker 心跳：best-effort 写入，不反向影响入库。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def record_job_event(
    engine: Engine,
    *,
    job_id: int,
    tenant_id: str,
    stage: str,
    status: str,
    progress: int | None = None,
    message: str = "",
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ingest_job_events "
                    "(job_id, tenant_id, stage, status, progress, message) "
                    "VALUES (:job_id, :tenant_id, :stage, :status, :progress, :message)"
                ),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "stage": stage,
                    "status": status,
                    "progress": progress,
                    "message": message[:1000],
                },
            )
    except Exception:  # noqa: BLE001 - 事件失败不能影响入库
        logger.exception("任务事件写入失败：job_id=%s stage=%s", job_id, stage)


def list_job_events(engine: Engine, tenant_id: str, job_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, job_id, tenant_id, stage, status, progress, message, created_at "
                "FROM ingest_job_events "
                "WHERE tenant_id = :tenant_id AND job_id = :job_id ORDER BY id"
            ),
            {"tenant_id": tenant_id, "job_id": job_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def record_worker_heartbeat(
    engine: Engine,
    *,
    worker_id: str,
    version: str = "",
    current_job_id: int | None = None,
) -> None:
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ops_workers "
                    "(worker_id, last_seen, version, current_job_id, updated_at) "
                    "VALUES (:worker_id, :now, :version, :job_id, :now) "
                    "ON CONFLICT (worker_id) DO UPDATE SET "
                    "last_seen = EXCLUDED.last_seen, "
                    "version = EXCLUDED.version, "
                    "current_job_id = EXCLUDED.current_job_id, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "worker_id": worker_id,
                    "now": datetime.now(timezone.utc),
                    "version": version,
                    "job_id": current_job_id,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception("worker 心跳写入失败：worker_id=%s", worker_id)


def list_workers(engine: Engine) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT worker_id, last_seen, version, current_job_id "
                "FROM ops_workers ORDER BY last_seen DESC"
            )
        ).mappings().all()
    now = datetime.now(timezone.utc)
    return [
        {
            **dict(row),
            "age_seconds": max(0.0, (now - row["last_seen"]).total_seconds()),
        }
        for row in rows
    ]
```

**Step 4: 接入任务状态机**

`src/ingest_jobs.py`：

- `_JOB_COLUMNS` 增加 `started_at`。
- `claim_next` 的 UPDATE 增加：

```sql
started_at = COALESCE(started_at, now()),
```

- 新增 `retry_job`：

```python
def retry_job(engine: Engine, tenant_id: str, job_id: int) -> dict | None:
    """只允许 failed 任务人工重试；重置 attempts/progress/error。"""
    with engine.begin() as connection:
        row = connection.execute(
            text(
                "UPDATE ingest_jobs "
                "SET status = 'queued', attempts = 0, progress = 0, error = '', "
                "started_at = NULL, finished_at = NULL, updated_at = now() "
                "WHERE id = :id AND tenant_id = :t AND status = 'failed' "
                f"RETURNING {_JOB_COLUMNS}"
            ),
            {"id": job_id, "t": tenant_id},
        ).mappings().first()
    return dict(row) if row is not None else None
```

**Step 5: 接入 worker 与 ingest 阶段**

`src/ingest.py` 的 `ingest_document` 增加可选回调：

```python
from collections.abc import Callable

StageCallback = Callable[[str, int, str], None]


def ingest_document(
    engine: Engine,
    *,
    tenant_id: str,
    kb_id: str,
    doc_id: str,
    path: str | Path,
    embed_client: Any,
    parser: Any = None,
    on_stage: StageCallback | None = None,
) -> dict:
    file_path = Path(path)

    def stage(name: str, progress: int, message: str = "") -> None:
        if on_stage is not None:
            on_stage(name, progress, message)

    stage("parsing", 30, "开始解析")
    text_content = _extract_text(file_path, parser)
    if not text_content.strip():
        raise ValueError(f"文档 {doc_id} 提取结果为空，无法入库")

    stage("chunking", 50, "开始切分")
    chunks = split_document(text_content, doc_id=doc_id)
    texts = [chunk.text for chunk in chunks if not chunk.is_parent]

    stage("embedding", 65, "开始嵌入")
    vectors = embed_client.embed_texts(texts)
    if len(vectors) != len(texts):
        raise ValueError(
            f"embedding 返回数量与子块数量不一致：期望 {len(texts)}，实际 {len(vectors)}"
        )

    stage("publishing", 90, "开始原子发布")
    # 原有 chunks/BM25/documents 原子发布逻辑保持不变
    return {"chunks": len(chunks), "doc_status": "ready"}
```

`src/worker.py`（保留现有 `UPLOAD_ROOT` 与错误处理语义，只插入心跳和阶段事件）：

```python
import os

from src.ingest_events import record_job_event, record_worker_heartbeat


def worker_once(
    engine: Engine, *, embed_client: Any, parser: Any = None
) -> int:
    worker_id = os.environ.get("WORKER_ID", "worker-local")
    job = claim_next(engine)
    record_worker_heartbeat(
        engine,
        worker_id=worker_id,
        current_job_id=job["id"] if job else None,
    )
    if job is None:
        return 0

    job_id = job["id"]

    def on_stage(stage: str, progress: int, message: str = "") -> None:
        mark_progress(engine, job_id, progress)
        record_job_event(
            engine,
            job_id=job_id,
            tenant_id=job["tenant_id"],
            stage=stage,
            status="started",
            progress=progress,
            message=message,
        )

    record_job_event(
        engine, job_id=job_id, tenant_id=job["tenant_id"],
        stage="claimed", status="started", progress=10,
    )

    try:
        with engine.begin() as connection:
            source_url = connection.execute(
                text(
                    "SELECT source_url FROM documents "
                    "WHERE id = :doc_id AND tenant_id = :tenant_id AND kb_id = :kb_id"
                ),
                {
                    "doc_id": job["doc_id"],
                    "tenant_id": job["tenant_id"],
                    "kb_id": job["kb_id"],
                },
            ).scalar_one_or_none()
        if not source_url:
            raise ValueError(
                f"文档 {job['doc_id']} 不存在或 source_url 为空，无法定位上传文件"
            )

        path = UPLOAD_ROOT / source_url
        mark_progress(engine, job_id, 30)
        result = ingest_document(
            engine,
            tenant_id=job["tenant_id"],
            kb_id=job["kb_id"],
            doc_id=job["doc_id"],
            path=path,
            embed_client=embed_client,
            parser=parser,
            on_stage=on_stage,
        )
        mark_succeeded(engine, job_id)
        record_job_event(
            engine, job_id=job_id, tenant_id=job["tenant_id"],
            stage="succeeded", status="succeeded", progress=100,
        )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        mark_failed(
            engine,
            job_id,
            error,
            retry=not isinstance(exc, NonRetryableIngestError),
        )
        record_job_event(
            engine, job_id=job_id, tenant_id=job["tenant_id"],
            stage="failed", status="failed", progress=0,
            message=error,
        )
        failed_job = get_job(engine, job["tenant_id"], job_id)
        if failed_job is not None and failed_job["status"] == "failed":
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE documents SET status = 'failed', updated_at = now() "
                        "WHERE id = :doc_id AND tenant_id = :tenant_id"
                    ),
                    {"doc_id": job["doc_id"], "tenant_id": job["tenant_id"]},
                )
        print(
            f"worker 入库失败：job_id={job_id} doc_id={job['doc_id']} error={error}",
            file=sys.stderr,
        )
    else:
        print(
            f"worker 入库成功：job_id={job_id} doc_id={job['doc_id']} "
            f"chunks={result['chunks']}"
        )
    finally:
        record_worker_heartbeat(engine, worker_id=worker_id, current_job_id=None)
    return 1
```

`scripts/run_worker.py` 在 `while True` 循环开头写心跳：

```python
from src.ingest_events import record_worker_heartbeat

worker_id = os.environ.get("WORKER_ID") or f"worker-{os.getpid()}"
while True:
    record_worker_heartbeat(engine, worker_id=worker_id)
    processed = worker_once(engine, embed_client=embed_client, parser=parser)
    if not processed:
        time.sleep(interval)
```

`app/routers/documents.py` 在文件写入和任务入队的事务提交后追加：

```python
from src.ingest_events import record_job_event

record_job_event(
    engine,
    job_id=job_id,
    tenant_id=tenant_id,
    stage="queued",
    status="started",
    progress=0,
    message=f"source=upload doc_id={doc_id}",
)
```

**Step 6: 跑测试与全量回归**

Run:

```bash
py -3.13 -m pytest tests/test_ingest_events.py tests/test_ingest_jobs.py tests/test_worker_connectors.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 7: Commit**

```bash
git add src/ingest_events.py tests/test_ingest_events.py src/ingest_jobs.py src/ingest.py src/worker.py scripts/run_worker.py app/routers/documents.py
git commit -m "feat(ops): record ingest stage events and worker heartbeats"
```

---

### Task 3: 身份卡与权限解释 API

**Files:**
- Create: `app/routers/identity.py`
- Modify: `app/main.py`
- Modify: `app/deps.py`（新增 `ops_actions_enabled`）
- Test: `tests/test_identity_api.py`
- Test: `tests/test_permissions_api.py`

**Step 1: 写失败测试**

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.db.base import create_db_engine

TENANT = "t_identity"
KB = "kb_identity"


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'identity')"), {"t": TENANT}
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'identity kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_admin', :t, 'Admin', ARRAY['dept:cs'], 'admin')"
            ),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO users (user_id, tenant_id, display_name, tags, role) "
                "VALUES ('u_member', :t, 'Member', ARRAY['dept:legal'], 'member')"
            ),
            {"t": TENANT},
        )
    yield db_engine
    with db_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    db_engine.dispose()


@pytest.fixture()
def client(engine) -> TestClient:
    return TestClient(create_app())


def test_identity_returns_role_and_tags(client: TestClient) -> None:
    response = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == "u_admin"
    assert body["role"] == "admin"
    assert body["is_admin"] is True
    assert body["tags"] == ["dept:cs"]
    assert KB in body["kbs"]
    assert body["isolation"]["rls"]["enabled"] in (True, False)


def test_unknown_user_falls_back_to_guest(client: TestClient) -> None:
    body = client.get(
        "/v1/identity",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "ghost"},
    ).json()
    assert body["role"] == "guest"
    assert body["is_admin"] is False
    assert body["tags"] == []


def test_permissions_cross_tenant_returns_404(client: TestClient) -> None:
    response = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": "t_other", "X-User-Id": "u_admin"},
    )
    assert response.status_code == 404


def test_permissions_reports_acl_mode_and_rls_counts(client: TestClient) -> None:
    body = client.get(
        "/v1/ops/permissions",
        params={"kb_id": KB},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    ).json()
    assert body["acl"]["mode"] == "public_or_tag_intersection"
    assert body["rls"]["bare_count"] == 0 or body["rls"]["enabled"] is False
    assert body["decision"] in {"allow", "deny"}
    assert body["reason"]
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_identity_api.py tests/test_permissions_api.py -q`

Expected: FAIL（404，路由不存在）。

**Step 3: 实现身份与权限**

`app/deps.py` 增加：

```python
def ops_actions_enabled() -> bool:
    return os.environ.get("OPS_ENABLE_ACTIONS", "0").strip() == "1"
```

`app/routers/identity.py`：

```python
"""身份卡与权限解释：只读、租户作用域、角色来自数据库。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.deps import EngineDep, TenantId, UserId, ops_actions_enabled
from app.services import ensure_kb
from src.acl import acl_condition, load_user_context
from src.db.session import tenant_scope

router = APIRouter(prefix="/v1", tags=["identity"])


def _user_row(engine, tenant_id: str, user_id: str) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT user_id, display_name, tags, role FROM users "
                "WHERE tenant_id = :t AND user_id = :u"
            ),
            {"t": tenant_id, "u": user_id},
        ).mappings().first()
    return dict(row) if row else {}


def _rls_metadata(engine) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'chunks' AND relkind = 'r'"
            )
        ).mappings().first()
        policies = connection.execute(
            text("SELECT count(*) FROM pg_policies WHERE tablename = 'chunks'")
        ).scalar_one()
    return {
        "enabled": bool(row["relrowsecurity"]) if row else False,
        "forced": bool(row["relforcerowsecurity"]) if row else False,
        "policy_count": int(policies),
    }


@router.get("/identity")
def identity(
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    user = _user_row(engine, tenant_id, user_id)
    role = str(user.get("role") or "guest")
    tags = list(user.get("tags") or [])
    is_admin = role == "admin"
    with engine.connect() as connection:
        kbs = connection.execute(
            text("SELECT id FROM knowledge_bases WHERE tenant_id = :t ORDER BY id"),
            {"t": tenant_id},
        ).scalars().all()
    actions_enabled = ops_actions_enabled()
    return {
        "tenant": tenant_id,
        "user": user_id,
        "role": role,
        "tags": tags,
        "is_admin": is_admin,
        "kbs": list(kbs),
        "isolation": {
            "tenant_scope": True,
            "acl": "admin_bypass" if is_admin else "public_or_tag_intersection",
            "rls": _rls_metadata(engine),
        },
        "ops": {
            "actions_enabled": actions_enabled,
            "can_trigger": bool(is_admin and actions_enabled),
        },
    }


@router.get("/ops/permissions")
def permissions(
    kb_id: str,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    user = _user_row(engine, tenant_id, user_id)
    role = str(user.get("role") or "guest")
    tags = list(user.get("tags") or [])
    is_admin = role == "admin"
    rls = _rls_metadata(engine)

    with engine.connect() as connection:
        bare_count = connection.execute(
            text("SELECT count(*) FROM chunks WHERE kb_id = :kb"),
            {"kb": kb_id},
        ).scalar_one()

    condition, params = acl_condition(tags, is_admin)
    with tenant_scope(engine, tenant_id) as connection:
        scoped_count = connection.execute(
            text(
                "SELECT count(*) FROM chunks c "
                "JOIN documents d ON d.id = c.doc_id AND d.tenant_id = c.tenant_id "
                "WHERE c.tenant_id = :t AND c.kb_id = :kb "
                "AND d.status = 'ready' AND " + condition
            ),
            {"t": tenant_id, "kb": kb_id, **params},
        ).scalar_one()

    decision = "allow" if scoped_count else "deny"
    reason = (
        "租户匹配，ACL 可见，RLS 已启用"
        if decision == "allow"
        else "租户匹配，但当前身份没有可见 chunk（ACL 为空或文档未 ready）"
    )
    return {
        "tenant": tenant_id,
        "user": user_id,
        "role": role,
        "tags": tags,
        "is_admin": is_admin,
        "kb": {"id": kb_id, "exists": True, "tenant_match": True},
        "acl": {
            "mode": "admin_bypass" if is_admin else "public_or_tag_intersection",
            "user_tags": tags,
        },
        "rls": {**rls, "bare_count": int(bare_count), "scoped_count": int(scoped_count)},
        "decision": decision,
        "reason": reason,
    }
```

`app/main.py` 注册 `identity.router`。

**Step 4: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_identity_api.py tests/test_permissions_api.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 5: Commit**

```bash
git add app/routers/identity.py app/main.py app/deps.py tests/test_identity_api.py tests/test_permissions_api.py
git commit -m "feat(ops): add identity and permission explanation APIs"
```

---

### Task 4: 驾驶舱聚合指标与摘要 API

**Files:**
- Create: `src/ops_metrics.py`
- Create: `src/ops_runs.py`（先只做只读函数）
- Create: `app/routers/ops.py`
- Modify: `app/main.py`
- Test: `tests/test_ops_metrics.py`
- Test: `tests/test_ops_summary_api.py`

**Step 1: 写失败测试**

```python
from datetime import datetime, timedelta, timezone

from src.ops_metrics import build_ops_summary, percentile, summarize_ai, summarize_queue


def test_percentile_interpolates() -> None:
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert percentile([], 0.95) is None


def test_queue_counts_wait_and_duration() -> None:
    now = datetime.now(timezone.utc)
    jobs = [
        {"status": "queued", "created_at": now - timedelta(seconds=10), "started_at": None,
         "finished_at": None},
        {"status": "succeeded", "created_at": now - timedelta(seconds=30),
         "started_at": now - timedelta(seconds=20), "finished_at": now - timedelta(seconds=5)},
        {"status": "failed", "created_at": now - timedelta(seconds=25),
         "started_at": now - timedelta(seconds=20), "finished_at": now - timedelta(seconds=2)},
    ]
    summary = summarize_queue(jobs)
    assert summary["queued"] == 1
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["oldest_queued_seconds"] >= 9
    assert summary["avg_duration_seconds"] == 16.5


def test_ai_metrics_use_real_denominators() -> None:
    traces = [
        {"stage": "retrieve", "hits": 0, "refused": False, "mode": "hybrid"},
        {"stage": "retrieve", "hits": 3, "refused": False, "mode": "hybrid"},
        {"stage": "answer", "hits": 3, "refused": False, "mode": "chat",
         "prompt_tokens": 100, "completion_tokens": 20, "llm_ms": 500,
         "citation_ok": True, "rerank_degraded": False},
        {"stage": "answer", "hits": 0, "refused": True, "mode": "chat",
         "prompt_tokens": 50, "completion_tokens": 0, "llm_ms": 300,
         "citation_ok": None, "rerank_degraded": True},
    ]
    summary = summarize_ai(traces)
    assert summary["requests"] == 2
    assert summary["refusal_rate"] == 0.5
    assert summary["zero_result_rate"] == 0.5
    assert summary["tokens"]["total"] == 170
    assert summary["llm_latency_ms"]["p50"] == 400
    assert summary["citation_ok_rate"] == 1.0
    assert summary["rerank_degraded_rate"] == 0.5


def test_build_summary_keeps_empty_values_nullable() -> None:
    summary = build_ops_summary(
        health={"api": "up", "db": "up", "parser": "unconfigured", "workers": []},
        jobs=[], traces=[], runs=[], workers=[],
    )
    assert summary["queue"]["total"] == 0
    assert summary["ai"]["requests"] == 0
    assert summary["ai"]["refusal_rate"] is None
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_metrics.py -q`

Expected: FAIL（模块不存在）。

**Step 3: 实现纯聚合**

`src/ops_metrics.py`：

```python
"""调测驾驶舱的纯聚合函数；数据加载由路由负责。"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_queue(jobs: list[dict]) -> dict:
    counts = Counter(job["status"] for job in jobs)
    now = datetime.now(timezone.utc)
    queued_at = [
        job["created_at"] for job in jobs
        if job["status"] == "queued" and job.get("created_at")
    ]
    waits = [
        (job["started_at"] - job["created_at"]).total_seconds()
        for job in jobs
        if job.get("started_at") and job.get("created_at")
    ]
    durations = [
        (job["finished_at"] - job["started_at"]).total_seconds()
        for job in jobs
        if job.get("finished_at") and job.get("started_at")
    ]
    errors = Counter(
        (job.get("error") or "").splitlines()[0][:200]
        for job in jobs if job.get("status") == "failed" and job.get("error")
    )
    return {
        "total": len(jobs),
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "succeeded": counts.get("succeeded", 0),
        "failed": counts.get("failed", 0),
        "oldest_queued_seconds": (
            max(0.0, (now - min(queued_at)).total_seconds()) if queued_at else None
        ),
        "avg_wait_seconds": round(sum(waits) / len(waits), 2) if waits else None,
        "avg_duration_seconds": round(sum(durations) / len(durations), 2)
        if durations else None,
        "retry_count": sum(max(0, int(job.get("attempts") or 0) - 1) for job in jobs),
        "top_errors": [
            {"error": error, "count": count}
            for error, count in errors.most_common(5)
        ],
    }


def summarize_ai(traces: list[dict]) -> dict:
    retrieves = [trace for trace in traces if trace.get("stage") == "retrieve"]
    answers = [trace for trace in traces if trace.get("stage") == "answer"]
    citations = [
        trace["citation_ok"] for trace in answers
        if trace.get("citation_ok") is not None
    ]
    reranks = [
        bool(trace.get("rerank_degraded")) for trace in answers
        if trace.get("rerank_degraded") is not None
    ]
    prompt = sum(int(trace.get("prompt_tokens") or 0) for trace in answers)
    completion = sum(int(trace.get("completion_tokens") or 0) for trace in answers)
    llm_latencies = [
        float(trace["llm_ms"]) for trace in answers if trace.get("llm_ms") is not None
    ]
    input_price = os.environ.get("LLM_INPUT_PRICE_PER_1K")
    output_price = os.environ.get("LLM_OUTPUT_PRICE_PER_1K")
    estimated_cost = None
    if input_price and output_price:
        estimated_cost = round(
            prompt / 1000 * float(input_price)
            + completion / 1000 * float(output_price),
            6,
        )
    return {
        "requests": len(answers),
        "refusal_rate": _ratio(
            sum(trace.get("refused") is True for trace in answers), len(answers)
        ),
        "zero_result_rate": _ratio(
            sum(int(trace.get("hits") or 0) == 0 for trace in retrieves),
            len(retrieves),
        ),
        "citation_ok_rate": _ratio(sum(bool(item) for item in citations), len(citations)),
        "rerank_degraded_rate": _ratio(sum(reranks), len(reranks)),
        "tokens": {
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
        },
        "llm_latency_ms": {
            "p50": percentile(llm_latencies, 0.50),
            "p95": percentile(llm_latencies, 0.95),
        },
        "model": next(
            (trace.get("model") for trace in reversed(answers) if trace.get("model")),
            None,
        ),
        "estimated_cost": estimated_cost,
    }


def build_ops_summary(
    *,
    health: dict,
    jobs: list[dict],
    traces: list[dict],
    runs: list[dict],
    workers: list[dict],
) -> dict:
    return {
        "health": health,
        "queue": summarize_queue(jobs),
        "ai": summarize_ai(traces),
        "latest_runs": runs[:5],
        "latest_traces": traces[-10:],
        "workers": workers,
    }
```

`src/ops_runs.py` 先实现只读：

```python
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine


def list_runs(
    engine: Engine, tenant_id: str, kb_id: str, *, limit: int = 20,
    kind: str | None = None,
) -> list[dict]:
    query = (
        "SELECT id, tenant_id, kb_id, kind, status, params, requested_by, "
        "requested_at, started_at, finished_at, exit_code, summary, "
        "artifact_path, output_tail, error, attempts "
        "FROM ops_runs WHERE tenant_id = :t AND kb_id = :kb"
    )
    params: dict[str, object] = {"t": tenant_id, "kb": kb_id, "limit": limit}
    if kind:
        query += " AND kind = :kind"
        params["kind"] = kind
    query += " ORDER BY id DESC LIMIT :limit"
    with engine.connect() as connection:
        rows = connection.execute(text(query), params).mappings().all()
    return [dict(row) for row in rows]


def get_run(engine: Engine, tenant_id: str, run_id: int) -> dict | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, tenant_id, kb_id, kind, status, params, requested_by, "
                "requested_at, started_at, finished_at, exit_code, summary, "
                "artifact_path, output_tail, error, attempts "
                "FROM ops_runs WHERE id = :id AND tenant_id = :t"
            ),
            {"id": run_id, "t": tenant_id},
        ).mappings().first()
    return dict(row) if row else None
```

**Step 4: 实现摘要路由**

`app/routers/ops.py`：

```python
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter

from app.deps import EngineDep, TenantId, TraceStoreDep
from app.services import ensure_kb
from src import ingest_jobs, ops_runs
from src.ingest_events import list_workers
from src.ops_metrics import build_ops_summary

router = APIRouter(prefix="/v1", tags=["ops"])


def _parser_health() -> str:
    base_url = os.environ.get("PARSER_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return "unconfigured"
    try:
        response = httpx.get(f"{base_url}/health", timeout=0.8)
        return "up" if response.status_code < 500 else "down"
    except Exception:  # noqa: BLE001
        return "down"


@router.get("/ops/summary")
def ops_summary(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    trace_store: TraceStoreDep,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=500)
    traces = trace_store.list_records(tenant_id, kb_id)
    workers = list_workers(engine)
    health = {
        "api": "up",
        "db": "up",
        "parser": _parser_health(),
        "workers": workers,
    }
    return build_ops_summary(
        health=health,
        jobs=jobs,
        traces=traces,
        runs=ops_runs.list_runs(engine, tenant_id, kb_id, limit=5),
        workers=workers,
    )
```

`app/main.py` 注册 `ops.router`。

**Step 5: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_ops_metrics.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add src/ops_metrics.py src/ops_runs.py app/routers/ops.py app/main.py tests/test_ops_metrics.py
git commit -m "feat(ops): add cockpit summary metrics API"
```

---

### Task 5: 任务看板、任务事件与 Trace 列表 API

**Files:**
- Modify: `src/trace.py`（可选指标字段 + `list_recent`）
- Modify: `app/routers/ops.py`
- Test: `tests/test_trace.py`（只追加，不改他人 dirty 测试）
- Create: `tests/test_ops_jobs_traces_api.py`

**Step 1: 写失败测试**

```python
from datetime import datetime, timezone

from src.trace import TraceStore

TENANT = "t_ops_trace"
KB = "kb_ops_trace"


def test_trace_record_keeps_optional_usage_fields(tmp_path) -> None:
    store = TraceStore(tmp_path)
    item = store.record(
        trace_id="abc",
        tenant=TENANT,
        kb=KB,
        stage="answer",
        ms=12.3,
        hits=2,
        refused=False,
        mode="chat",
        query="退款",
        prompt_tokens=100,
        completion_tokens=20,
        model="deepseek-chat",
        llm_ms=500.0,
        rerank_degraded=False,
        citation_ok=True,
    )
    assert item["prompt_tokens"] == 100
    assert item["completion_tokens"] == 20
    assert item["model"] == "deepseek-chat"
    assert item["citation_ok"] is True


def test_old_trace_without_usage_fields_still_readable(tmp_path) -> None:
    store = TraceStore(tmp_path)
    store.record(
        trace_id="old", tenant=TENANT, kb=KB, stage="retrieve",
        ms=1.0, hits=1, refused=False, mode="hybrid", query="旧记录",
    )
    stages = store.find("old", TENANT)
    assert stages[0].get("prompt_tokens") is None


def test_list_recent_returns_latest_first(tmp_path) -> None:
    store = TraceStore(tmp_path)
    for index in range(3):
        store.record(
            trace_id=f"trace-{index}", tenant=TENANT, kb=KB,
            stage="retrieve", ms=1.0, hits=index, refused=False,
            mode="hybrid", query=f"q{index}",
        )
    recent = store.list_recent(TENANT, KB, limit=2)
    assert [item["trace_id"] for item in recent] == ["trace-2", "trace-1"]
```

API 测试：

```python
def test_ops_jobs_returns_wait_and_duration(client, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_ops_job")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='succeeded', started_at=now(), "
                "finished_at=now() + interval '2 seconds', progress=100 WHERE id=:id"
            ),
            {"id": job_id},
        )

    body = client.get(
        "/v1/ops/jobs", params={"kb_id": KB}, headers={"X-Tenant-Id": TENANT}
    ).json()
    job = next(item for item in body["jobs"] if item["id"] == job_id)
    assert job["wait_seconds"] is not None
    assert job["duration_seconds"] == 2.0


def test_ops_job_events_are_tenant_scoped(client, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_ops_events")
    record_job_event(
        engine, job_id=job_id, tenant_id=TENANT,
        stage="parsing", status="started", progress=30,
    )
    body = client.get(
        f"/v1/ops/jobs/{job_id}/events", headers={"X-Tenant-Id": TENANT}
    ).json()
    assert body["events"][0]["stage"] == "parsing"
    assert client.get(
        f"/v1/ops/jobs/{job_id}/events", headers={"X-Tenant-Id": "other"}
    ).status_code == 404


def test_ops_traces_returns_recent_records(client, tmp_path) -> None:
    from app.deps import get_trace_store
    from src.trace import TraceStore

    store = TraceStore(tmp_path)
    store.record(
        trace_id="trace-api", tenant=TENANT, kb=KB, stage="retrieve",
        ms=1.0, hits=2, refused=False, mode="hybrid", query="退款",
    )
    client.app.dependency_overrides[get_trace_store] = lambda: store
    try:
        body = client.get(
            "/v1/ops/traces",
            params={"kb_id": KB},
            headers={"X-Tenant-Id": TENANT},
        ).json()
    finally:
        client.app.dependency_overrides.pop(get_trace_store, None)
    assert body["traces"][0]["trace_id"] == "trace-api"
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_jobs_traces_api.py -q`

Expected: FAIL（`list_recent` 不存在 / 路由 404）。

**Step 3: 扩展 TraceStore**

`src/trace.py`：

```python
from datetime import datetime, timedelta, timezone

    def record(
        self,
        *,
        trace_id: str,
        tenant: str,
        kb: str,
        stage: str,
        ms: float,
        hits: int,
        refused: bool,
        mode: str,
        query: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        model: str | None = None,
        llm_ms: float | None = None,
        rerank_degraded: bool | None = None,
        citation_ok: bool | None = None,
    ) -> dict:
        now = datetime.now(timezone.utc)
        item = {
            "trace_id": trace_id,
            "tenant": tenant,
            "kb": kb,
            "stage": stage,
            "ms": round(max(0.0, float(ms)), 3),
            "hits": hits,
            "refused": refused,
            "mode": mode,
            "query": query,
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        optional = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": model,
            "llm_ms": round(float(llm_ms), 3) if llm_ms is not None else None,
            "rerank_degraded": rerank_degraded,
            "citation_ok": citation_ok,
        }
        item.update({key: value for key, value in optional.items() if value is not None})
        with self._memory_lock:
            item["seq"] = self._seq
            self._seq += 1
            self._records.append(item)
        try:
            with self._file_lock:
                self._root.mkdir(parents=True, exist_ok=True)
                path = self._root / f"{now.date().isoformat()}.jsonl"
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
        except OSError:
            logger.exception(
                "trace 写盘失败，已保留内存记录：trace_id=%s", trace_id
            )
        return dict(item)
```

`list_recent` 追加到 `TraceStore`：

```python
    def list_recent(
        self, tenant: str, kb: str, *, limit: int = 50, days: int = 7
    ) -> list[dict]:
        records = self.list_records(tenant, kb)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        def parsed_ts(item: dict) -> datetime:
            return datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))

        fresh = [item for item in records if parsed_ts(item) >= cutoff]
        return list(reversed(fresh[-limit:]))
```

**Step 4: 增加 ops 路由**

`app/routers/ops.py`：

```python
from fastapi import HTTPException

from src.ingest_events import list_job_events


def _job_metrics(job: dict) -> dict:
    created = job.get("created_at")
    started = job.get("started_at")
    finished = job.get("finished_at")
    job["wait_seconds"] = (
        round((started - created).total_seconds(), 3)
        if created and started else None
    )
    job["duration_seconds"] = (
        round((finished - started).total_seconds(), 3)
        if started and finished else None
    )
    return job


@router.get("/ops/jobs")
def ops_jobs(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    all_jobs = ingest_jobs.list_jobs(engine, tenant_id, kb_id, limit=500)
    counts = {
        key: sum(1 for job in all_jobs if job["status"] == key)
        for key in ("queued", "running", "succeeded", "failed")
    }
    jobs = all_jobs
    if status:
        jobs = [job for job in jobs if job["status"] == status]
    jobs = [_job_metrics(job) for job in jobs[: max(1, min(limit, 200))]]
    return {"jobs": jobs, "counts": counts}


@router.get("/ops/jobs/{job_id}/events")
def ops_job_events(
    job_id: int,
    tenant_id: TenantId,
    engine: EngineDep,
) -> dict:
    job = ingest_jobs.get_job(engine, tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "events": list_job_events(engine, tenant_id, job_id)}


@router.get("/ops/traces")
def ops_traces(
    kb_id: str,
    tenant_id: TenantId,
    engine: EngineDep,
    trace_store: TraceStoreDep,
    limit: int = 20,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    return {
        "traces": trace_store.list_recent(
            tenant_id, kb_id, limit=max(1, min(limit, 200))
        )
    }
```

**Step 5: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_trace.py tests/test_ops_jobs_traces_api.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add src/trace.py app/routers/ops.py tests/test_trace.py tests/test_ops_jobs_traces_api.py
git commit -m "feat(ops): expose job timeline and recent traces"
```

---

### Task 6: Chat usage/timing 采集与 SSE 透传

**Files:**
- Create: `app/observability.py`
- Modify: `app/routers/chat.py`
- Test: `tests/test_chat_usage.py`

**Step 1: 写失败测试**

```python
from types import SimpleNamespace

from app.observability import TimedRetriever, UsageRecordingChatClient, usage_from_chat_result


def test_usage_recorder_keeps_last_chat_result() -> None:
    result = SimpleNamespace(prompt_tokens=12, completion_tokens=7, model="m", latency_ms=100)
    inner = SimpleNamespace(chat=lambda *args, **kwargs: result)
    wrapper = UsageRecordingChatClient(inner)
    assert wrapper.chat("q") is result
    assert wrapper.last is result


def test_timed_retriever_records_last_ms() -> None:
    inner = SimpleNamespace(retrieve=lambda *args, **kwargs: ["hit"])
    wrapper = TimedRetriever(inner)
    assert wrapper.retrieve("q") == ["hit"]
    assert wrapper.last_ms >= 0
```

纯函数测试：

```python
def test_usage_from_chat_result_builds_totals() -> None:
    result = SimpleNamespace(
        prompt_tokens=100, completion_tokens=20,
        model="deepseek-chat", latency_ms=500,
    )
    usage = usage_from_chat_result(result)
    assert usage["total_tokens"] == 120
    assert usage["model"] == "deepseek-chat"
    assert usage["llm_ms"] == 500
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_chat_usage.py -q`

Expected: FAIL（模块不存在）。

**Step 3: 实现包装器**

`app/observability.py`：

```python
"""不改 pipeline 的前提下采集 chat usage 与检索耗时。"""

from __future__ import annotations

import time
from typing import Any


class UsageRecordingChatClient:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.last: Any = None

    def chat(self, *args, **kwargs):
        result = self.inner.chat(*args, **kwargs)
        self.last = result
        return result


class TimedRetriever:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.last_ms: float = 0.0

    def retrieve(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self.inner.retrieve(*args, **kwargs)
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000


def usage_from_chat_result(result: Any) -> dict:
    prompt = int(getattr(result, "prompt_tokens", 0) or 0)
    completion = int(getattr(result, "completion_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "model": getattr(result, "model", "") or "",
        "llm_ms": float(getattr(result, "latency_ms", 0.0) or 0.0),
    }
```

**Step 4: 接入 chat 路由**

`app/routers/chat.py`：

```python
from app.observability import TimedRetriever, UsageRecordingChatClient, usage_from_chat_result

    usage_client = UsageRecordingChatClient(chat_client)
    pipeline = build_pipeline(
        engine, tenant_id, payload.kb_id, embed_client, usage_client, reranker
    )
    timed_retriever = TimedRetriever(pipeline.retriever)
    pipeline.retriever = timed_retriever
```

`record_stage` 增加可选参数：

```python
        async def record_stage(
            stage: str, *, hits: int, refused: bool, mode: str,
            usage: dict | None = None, flags: dict | None = None,
        ) -> None:
            extra = {}
            if usage:
                extra.update(
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    model=usage.get("model"),
                    llm_ms=usage.get("llm_ms"),
                )
            if flags:
                extra.update(
                    rerank_degraded=flags.get("rerank_degraded"),
                    citation_ok=flags.get("citation_ok"),
                )
            await run_in_threadpool(
                trace_store.record,
                trace_id=trace_id,
                tenant=tenant_id,
                kb=payload.kb_id,
                stage=stage,
                ms=(time.perf_counter() - started) * 1000,
                hits=hits,
                refused=refused,
                mode=mode,
                query=payload.question,
                **extra,
            )
```

调用结束后组装：

```python
            chat_result = usage_client.last
            usage = usage_from_chat_result(chat_result)
            timings = {
                "retrieval_ms": round(timed_retriever.last_ms, 3),
                "llm_ms": round(float(usage["llm_ms"] or 0), 3),
            }
            await record_stage(
                "answer", hits=len(chunks), refused=refused, mode="chat",
                usage=usage,
                flags={
                    "rerank_degraded": result.get("rerank_degraded", False),
                    "citation_ok": result.get("citation_ok", True),
                },
            )
            yield (
                "event: hits\ndata: "
                + json.dumps(
                    {
                        "chunks": chunks,
                        "rewritten": result["rewritten"],
                        "rerank_degraded": result.get("rerank_degraded", False),
                        "citation_ok": result.get("citation_ok", True),
                        "usage": usage,
                        "timings": timings,
                        "trace_id": trace_id,
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
```

`rerank_degraded`/`citation_ok` 通过 `flags` 写入 trace；`timings` 只放进 SSE `hits` 帧，不进 trace。

**Step 5: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_chat_usage.py tests/test_trace.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add app/observability.py app/routers/chat.py tests/test_chat_usage.py
git commit -m "feat(ops): capture chat usage and timing in traces"
```

---

### Task 7: 微信桌面版三栏 Shell

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_api_ui.py`（只追加）

**Step 1: 写失败测试**

```python
def test_ui_has_wechat_desktop_shell(client) -> None:
    html = client.get("/ui/").text
    for marker in (
        'class="wx-rail"',
        'data-nav="workbench"',
        'data-nav="cockpit"',
        'data-nav="identity"',
        'data-nav="runs"',
        'data-view="workbench"',
        'class="wx-content"',
    ):
        assert marker in html
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

Expected: FAIL（shell 标记不存在）。

**Step 3: 重构 HTML 外层**

把现有 `<body>` 结构调整为：

```html
<body>
  <div class="wx-app">
    <nav class="wx-rail" aria-label="主导航">
      <div class="wx-brand">KB</div>
      <button class="wx-nav is-active" data-nav="workbench" title="工作台">
        <span class="wx-nav-icon">⌂</span><span class="wx-nav-label">工作台</span>
      </button>
      <button class="wx-nav" data-nav="cockpit" title="驾驶舱">
        <span class="wx-nav-icon">▤</span><span class="wx-nav-label">驾驶舱</span>
      </button>
      <button class="wx-nav" data-nav="identity" title="身份与权限">
        <span class="wx-nav-icon">◎</span><span class="wx-nav-label">身份</span>
      </button>
      <button class="wx-nav" data-nav="runs" title="评测与压测">
        <span class="wx-nav-icon">◫</span><span class="wx-nav-label">评测</span>
      </button>
      <button class="wx-nav" data-nav="audit" title="审计">
        <span class="wx-nav-icon">≡</span><span class="wx-nav-label">审计</span>
      </button>
    </nav>

    <aside class="wx-list" id="wx-list" aria-label="列表">
      <div class="wx-list-head" id="wx-list-title">工作台</div>
      <div class="wx-list-body" id="wx-list-body"></div>
    </aside>

    <div class="wx-main">
      <header class="wx-topbar">
        <div>
          <div class="wx-title">知识运维工作台</div>
          <div class="wx-subtitle" id="wx-scope">tenant / kb</div>
        </div>
        <div class="wx-badges" id="wx-badges"></div>
      </header>
      <main class="wx-content">
        <section data-view="workbench">
          <!-- 现有六块业务内容整体移到这里，保留所有 id 和 JS -->
        </section>
        <section data-view="cockpit" hidden></section>
        <section data-view="identity" hidden></section>
        <section data-view="runs" hidden></section>
        <section data-view="audit" hidden></section>
      </main>
    </div>
  </div>
</body>
```

注意：

- 现有 `masthead` 的标题/租户/KB 信息移到 `wx-topbar`，但保留原元素 id（例如 `tenant`、`kb` 输入）避免 JS 失效。
- 现有 `<main id="workbench">` 的内容整体放进 `section[data-view="workbench"]`；`id="workbench"` 保留在 section 上。
- 现有 footer 可放在 workbench section 末尾或删除；不要保留重复的全局 footer。

**Step 4: 加视觉 token 和布局 CSS**

在 `<style>` 顶部加 `:root`：

```css
:root {
  --wx-bg: #ededed;
  --wx-surface: #ffffff;
  --wx-rail: #2e2e2e;
  --wx-rail-active: #3a3a3a;
  --wx-list: #f7f7f7;
  --wx-primary: #07c160;
  --wx-primary-hover: #06ad56;
  --wx-link: #576b95;
  --wx-text: #191919;
  --wx-text-secondary: #7f7f7f;
  --wx-text-tertiary: #b2b2b2;
  --wx-divider: #e5e5e5;
  --wx-warning: #fa9d3b;
  --wx-danger: #fa5151;
  --wx-neutral: #888888;
  --wx-radius: 8px;
  --wx-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}

.wx-app { display: grid; grid-template-columns: 64px 320px minmax(0, 1fr); min-height: 100vh; background: var(--wx-bg); }
.wx-rail { background: var(--wx-rail); color: #fff; display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 12px 0; }
.wx-brand { width: 36px; height: 36px; border-radius: 8px; background: var(--wx-primary); display: grid; place-items: center; font-weight: 600; margin-bottom: 12px; }
.wx-nav { width: 56px; padding: 8px 0; border: 0; background: transparent; color: #d9d9d9; border-radius: 6px; cursor: pointer; display: grid; gap: 2px; justify-items: center; font-size: 11px; position: relative; }
.wx-nav:hover { background: #383838; }
.wx-nav.is-active { background: var(--wx-rail-active); color: #fff; }
.wx-nav.is-active::before { content: ""; position: absolute; left: 0; top: 8px; bottom: 8px; width: 3px; border-radius: 0 2px 2px 0; background: var(--wx-primary); }
.wx-nav-icon { font-size: 18px; line-height: 1; }
.wx-list { background: var(--wx-list); border-right: 1px solid var(--wx-divider); min-width: 0; display: flex; flex-direction: column; }
.wx-list-head { height: 56px; display: flex; align-items: center; padding: 0 16px; font-weight: 600; border-bottom: 1px solid var(--wx-divider); }
.wx-list-body { overflow: auto; }
.wx-main { min-width: 0; display: flex; flex-direction: column; }
.wx-topbar { min-height: 56px; background: var(--wx-surface); border-bottom: 1px solid var(--wx-divider); display: flex; align-items: center; justify-content: space-between; padding: 8px 24px; gap: 16px; }
.wx-title { font-size: 20px; font-weight: 600; color: var(--wx-text); }
.wx-subtitle { font-size: 12px; color: var(--wx-text-secondary); margin-top: 2px; }
.wx-badges { display: flex; gap: 8px; flex-wrap: wrap; }
.wx-content { flex: 1; overflow: auto; padding: 24px; }
.wx-content > [data-view] { max-width: 1440px; margin: 0 auto; }
.wx-card { background: var(--wx-surface); border: 1px solid var(--wx-divider); border-radius: var(--wx-radius); box-shadow: var(--wx-shadow); padding: 16px 20px; }
.wx-badge { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 4px; font-size: 12px; border: 1px solid var(--wx-divider); color: var(--wx-text-secondary); background: #fafafa; }
.wx-badge.is-real { color: var(--wx-primary); border-color: rgba(7, 193, 96, 0.45); background: #f2fbf6; }
.wx-badge.is-danger { color: var(--wx-danger); border-color: rgba(250, 81, 81, 0.4); background: #fff5f5; }

@media (max-width: 899px) {
  .wx-app { grid-template-columns: 1fr; }
  .wx-rail { flex-direction: row; height: 56px; padding: 0 8px; overflow-x: auto; }
  .wx-brand { margin: 0 8px 0 0; }
  .wx-nav { min-width: 64px; }
  .wx-nav.is-active::before { left: 8px; right: 8px; top: auto; bottom: 0; width: auto; height: 3px; border-radius: 2px 2px 0 0; }
  .wx-list { display: none; }
  .wx-content { padding: 16px; }
}
```

**Step 5: 加视图切换 JS**

在现有脚本末尾加：

```javascript
const navButtons = document.querySelectorAll("[data-nav]");
const views = document.querySelectorAll("[data-view]");

function showView(name) {
  views.forEach((view) => {
    view.hidden = view.dataset.view !== name;
  });
  navButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.nav === name);
  });
  const titleMap = {
    workbench: "工作台",
    cockpit: "调测驾驶舱",
    identity: "身份与权限",
    runs: "评测与压测",
    audit: "操作审计",
  };
  document.getElementById("wx-list-title").textContent = titleMap[name] || name;
  if (name === "cockpit" && typeof loadOpsSummary === "function") loadOpsSummary();
  if (name === "identity" && typeof loadIdentity === "function") loadIdentity();
  if (name === "runs" && typeof loadRuns === "function") loadRuns();
  if (name === "audit" && typeof loadAudit === "function") loadAudit();
}

navButtons.forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.nav));
});
```

**Step 6: 跑测试并手工验证**

Run:

```bash
py -3.13 -m pytest tests/test_api_ui.py -q
py -3.13 -m pytest -q
```

手工打开 `/ui`，确认：

- 三栏结构出现，左导航深色、选中态绿色；
- 现有选库、上传、检索、问答、引用仍可用；
- 900px 以下折叠为顶部导航。

**Step 7: Commit**

```bash
git add app/static/index.html tests/test_api_ui.py
git commit -m "feat(ui): add WeChat desktop shell"
```

---

### Task 8: 驾驶舱只读面板

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_api_ui.py`（只追加）

**Step 1: 写失败测试**

```python
def test_ui_has_cockpit_panels(client) -> None:
    html = client.get("/ui/").text
    for marker in (
        'id="ops-health"',
        'id="ops-queue"',
        'id="ops-jobs"',
        'id="ops-job-events"',
        'id="ops-ai"',
        'id="ops-traces"',
        'id="ops-trace-waterfall"',
    ):
        assert marker in html
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

Expected: FAIL。

**Step 3: 加驾驶舱 HTML**

在 `section[data-view="cockpit"]` 中加：

```html
<div class="wx-grid">
  <section class="wx-card" id="ops-health">
    <div class="wx-card-head"><h2>系统健康</h2><span class="wx-badge" id="ops-health-badge">-</span></div>
    <div class="wx-kv" id="ops-health-body"></div>
  </section>
  <section class="wx-card" id="ops-queue">
    <div class="wx-card-head"><h2>任务队列</h2><span class="wx-badge" id="ops-queue-badge">-</span></div>
    <div class="wx-metrics" id="ops-queue-body"></div>
    <svg class="wx-sparkline" id="ops-queue-sparkline" viewBox="0 0 240 48" aria-label="队列趋势"></svg>
  </section>
  <section class="wx-card wx-span-2" id="ops-ai">
    <div class="wx-card-head"><h2>AI 指标</h2><span class="wx-badge" id="ops-ai-badge">最近 7 天</span></div>
    <div class="wx-metrics" id="ops-ai-body"></div>
  </section>
  <section class="wx-card wx-span-2" id="ops-jobs">
    <div class="wx-card-head"><h2>任务看板</h2><span class="wx-badge" id="ops-jobs-badge">-</span></div>
    <div class="wx-job-board" id="ops-jobs-body"></div>
  </section>
  <section class="wx-card wx-span-2" id="ops-job-events">
    <div class="wx-card-head"><h2>任务时间线</h2><span class="wx-badge" id="ops-job-events-badge">未选择</span></div>
    <div id="ops-job-events-body" class="wx-timeline"></div>
  </section>
  <section class="wx-card wx-span-2" id="ops-traces">
    <div class="wx-card-head"><h2>最近 Trace</h2><span class="wx-badge" id="ops-traces-badge">-</span></div>
    <div class="wx-trace-layout">
      <div id="ops-traces-body" class="wx-trace-list"></div>
      <div id="ops-trace-waterfall" class="wx-waterfall"></div>
    </div>
  </section>
</div>
```

CSS 追加 `.wx-grid` 两列、`.wx-span-2` 跨列、`.wx-metrics` 四列、`.wx-job-board` 四列；`<900px` 全部单列。

**Step 4: 加数据加载与渲染 JS**

```javascript
const opsState = {
  summary: null,
  jobs: [],
  traces: [],
  selectedJobId: null,
  selectedTraceId: null,
  timer: null,
};

async function opsJson(path, options = {}) {
  const scope = currentScope();
  const response = await checkedFetch(path, {
    ...options,
    headers: {
      ...requestHeaders(scope, true),
      ...(options.headers || {}),
    },
  });
  return response.json();
}

function fmtSeconds(value) {
  return value == null ? "-" : `${Number(value).toFixed(1)}s`;
}

function fmtPercent(value) {
  return value == null ? "-" : `${(Number(value) * 100).toFixed(2)}%`;
}

function fmtMs(value) {
  return value == null ? "-" : `${Number(value).toFixed(0)}ms`;
}

function fmtNumber(value) {
  return value == null ? "-" : Number(value).toFixed(4);
}

async function loadOpsSummary() {
  const data = await opsJson(
    `/v1/ops/summary?kb_id=${encodeURIComponent(currentScope().kb)}`
  );
  opsState.summary = data;
  renderHealth(data.health);
  renderQueue(data.queue);
  renderAi(data.ai);
  await loadOpsJobs();
  await loadOpsTraces();
}

async function loadOpsJobs() {
  const data = await opsJson(
    `/v1/ops/jobs?kb_id=${encodeURIComponent(currentScope().kb)}&limit=50`
  );
  opsState.jobs = data.jobs;
  renderJobs(data.jobs, data.counts);
}

async function loadJobEvents(jobId) {
  opsState.selectedJobId = jobId;
  const data = await opsJson(`/v1/ops/jobs/${jobId}/events`);
  renderTimeline(data.events);
}

async function loadOpsTraces() {
  const data = await opsJson(
    `/v1/ops/traces?kb_id=${encodeURIComponent(currentScope().kb)}&limit=50`
  );
  opsState.traces = data.traces;
  renderTraceList(data.traces);
}

function renderHealth(health) {
  document.getElementById("ops-health-badge").textContent =
    health.parser === "up" ? "正常" : health.parser;
  document.getElementById("ops-health-body").innerHTML = [
    ["API", health.api],
    ["数据库", health.db],
    ["parser", health.parser],
    ["worker", (health.workers || []).map((w) => `${w.worker_id} · ${Math.round(w.age_seconds)}s`).join("<br>") || "无心跳"],
  ].map(([key, value]) => `<div><span>${key}</span><strong>${value}</strong></div>`).join("");
}

function renderQueue(queue) {
  document.getElementById("ops-queue-badge").textContent = `${queue.total} 个任务`;
  document.getElementById("ops-queue-body").innerHTML = [
    ["排队", queue.queued], ["运行", queue.running], ["成功", queue.succeeded],
    ["失败", queue.failed], ["最老等待", fmtSeconds(queue.oldest_queued_seconds)],
    ["平均耗时", fmtSeconds(queue.avg_duration_seconds)],
  ].map(([label, value]) => `<div class="wx-metric"><span>${label}</span><strong>${value ?? "-"}</strong></div>`).join("");
}

function renderAi(ai) {
  document.getElementById("ops-ai-body").innerHTML = [
    ["请求数", ai.requests],
    ["拒答率", fmtPercent(ai.refusal_rate)],
    ["引用通过率", fmtPercent(ai.citation_ok_rate)],
    ["rerank 降级", fmtPercent(ai.rerank_degraded_rate)],
    ["Token 总量", ai.tokens.total],
    ["LLM p95", fmtMs(ai.llm_latency_ms.p95)],
    ["模型", ai.model || "-"],
    ["估算成本", ai.estimated_cost == null ? "未配置价格" : ai.estimated_cost],
  ].map(([label, value]) => `<div class="wx-metric"><span>${label}</span><strong>${value ?? "-"}</strong></div>`).join("");
}

function renderJobs(jobs, counts) {
  document.getElementById("ops-jobs-badge").textContent =
    `排队 ${counts.queued} · 运行 ${counts.running} · 失败 ${counts.failed}`;
  document.getElementById("ops-jobs-body").innerHTML = jobs.map((job) => `
    <button class="wx-job ${job.status}" data-job-id="${job.id}">
      <span class="wx-job-id">#${job.id}</span>
      <span class="wx-job-doc">${job.doc_id || "-"}</span>
      <span class="wx-job-status">${job.status}</span>
      <span class="wx-job-meta">${job.progress || 0}% · ${job.attempts || 0} 次</span>
      <span class="wx-job-wait">等待 ${fmtSeconds(job.wait_seconds)} · 耗时 ${fmtSeconds(job.duration_seconds)}</span>
    </button>
  `).join("") || '<div class="wx-empty">暂无任务</div>';
  document.querySelectorAll("[data-job-id]").forEach((button) => {
    button.addEventListener("click", () => loadJobEvents(Number(button.dataset.jobId)));
  });
}
```

**Step 5: 时间线与 Trace 瀑布**

把上面的 `loadJobEvents` 改成传入 `jobId`，并追加三个渲染函数：

```javascript
async function loadJobEvents(jobId) {
  opsState.selectedJobId = jobId;
  const data = await opsJson(`/v1/ops/jobs/${jobId}/events`);
  renderTimeline(jobId, data.events);
}

function renderTimeline(jobId, events) {
  document.getElementById("ops-job-events-badge").textContent = `#${jobId} · ${events.length} 阶段`;
  document.getElementById("ops-job-events-body").innerHTML = events.map((event) => `
    <div class="wx-timeline-item ${event.status}">
      <span class="wx-timeline-dot"></span>
      <div>
        <strong>${event.stage}</strong>
        <span class="wx-timeline-status">${event.status}</span>
        <span class="wx-timeline-meta">
          ${event.progress == null ? "-" : `${event.progress}%`} · ${event.created_at}
        </span>
        ${event.message ? `<p>${escapeHtml(event.message)}</p>` : ""}
      </div>
    </div>
  `).join("") || '<div class="wx-empty">暂无阶段事件</div>';
}

function renderTraceList(traces) {
  const groups = new Map();
  for (const trace of traces) {
    if (!groups.has(trace.trace_id)) groups.set(trace.trace_id, []);
    groups.get(trace.trace_id).push(trace);
  }
  document.getElementById("ops-traces-badge").textContent = `${groups.size} 条`;
  document.getElementById("ops-traces-body").innerHTML =
    [...groups.entries()].map(([traceId, stages]) => `
      <button class="wx-list-row" data-trace-id="${traceId}">
        <span>${traceId}</span>
        <span>${escapeHtml(stages[0].query || "")}</span>
        <span class="wx-badge ${stages.some((item) => item.refused) ? "is-danger" : "is-real"}">
          ${stages.some((item) => item.refused) ? "拒答" : "回答"} · ${stages.length} 阶段
        </span>
      </button>
    `).join("") || '<div class="wx-empty">暂无 trace</div>';
  for (const button of document.querySelectorAll("[data-trace-id]")) {
    button.addEventListener("click", () => {
      renderWaterfall(groups.get(button.dataset.traceId) || []);
    });
  }
}

function renderWaterfall(stages) {
  const total = stages.reduce((sum, stage) => sum + Number(stage.ms || 0), 0) || 1;
  document.getElementById("ops-trace-waterfall").innerHTML = stages.length
    ? `
      ${stages.map((stage) => {
        const width = Math.max(2, (Number(stage.ms || 0) / total) * 100);
        return `
          <div class="wx-waterfall-row">
            <span class="wx-waterfall-label">${stage.stage}</span>
            <svg viewBox="0 0 100 12" preserveAspectRatio="none" class="wx-waterfall-bar">
              <rect x="0" y="0" width="${width}" height="12" rx="3"></rect>
            </svg>
            <span class="wx-waterfall-value">
              ${fmtMs(stage.ms)} · ${stage.hits} 命中${stage.refused ? " · 拒答" : ""}
            </span>
          </div>
        `;
      }).join("")}
      <p class="wx-hint">总耗时 ${fmtMs(total)}（柱宽按阶段耗时占比）</p>
    `
    : '<div class="wx-empty">点击左侧 Trace 查看阶段耗时</div>';
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"]/g, (char) => {
    if (char === "&") return "&amp;";
    if (char === "<") return "&lt;";
    if (char === ">") return "&gt;";
    return "&quot;";
  });
}
```

CSS 追加 `.wx-timeline`、`.wx-timeline-item`、`.wx-trace-layout`（左列表右瀑布，`<900px` 上下堆叠）、`.wx-waterfall-row`（`grid-template-columns: 90px 1fr 180px`）。

**Step 6: 轮询**

```javascript
function startOpsPolling() {
  stopOpsPolling();
  opsState.timer = setInterval(() => {
    if (document.visibilityState !== "visible") return;
    if (!document.querySelector('[data-view="cockpit"]').hidden) loadOpsSummary();
    if (opsState.selectedJobId) loadJobEvents(opsState.selectedJobId);
  }, 3000);
}

function stopOpsPolling() {
  if (opsState.timer) clearInterval(opsState.timer);
  opsState.timer = null;
}
```

在 Task 7 的 `showView(name)` 里，把 cockpit 分支改成同时管理轮询：

```javascript
if (name === "cockpit") {
  if (typeof loadOpsSummary === "function") loadOpsSummary();
  if (typeof startOpsPolling === "function") startOpsPolling();
} else if (typeof stopOpsPolling === "function") {
  stopOpsPolling();
}
```

切换 tenant/kb 时清空 `opsState` 并重新加载（`loadOpsSummary` 内部已按 `currentScope()` 取数）。

**Step 7: 跑测试并手工验证**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

手工验证：队列数字与 `/v1/ops/jobs` 一致；点击任务出现阶段时间线；Trace 列表可点开瀑布；无数据时显示空态。

**Step 8: Commit**

```bash
git add app/static/index.html tests/test_api_ui.py
git commit -m "feat(ui): add read-only cockpit panels"
```

---

### Task 9: 身份与权限只读面板

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_api_ui.py`（只追加）

**Step 1: 写失败测试**

```python
def test_ui_has_identity_panels(client) -> None:
    html = client.get("/ui/").text
    for marker in (
        'id="identity-card"',
        'id="identity-tags"',
        'id="identity-kbs"',
        'id="isolation-layers"',
        'id="permission-reason"',
        'id="identity-compare"',
    ):
        assert marker in html
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

Expected: FAIL。

**Step 3: 加身份 HTML**

在 `section[data-view="identity"]` 中加：

```html
<div class="wx-grid">
  <section class="wx-card" id="identity-card">
    <div class="wx-card-head"><h2>身份卡</h2><span class="wx-badge" id="identity-role">-</span></div>
    <div class="wx-kv" id="identity-body"></div>
    <div class="wx-tags" id="identity-tags"></div>
  </section>
  <section class="wx-card" id="identity-kbs">
    <div class="wx-card-head"><h2>可见知识库</h2><span class="wx-badge" id="identity-kb-count">-</span></div>
    <div class="wx-list-simple" id="identity-kb-body"></div>
  </section>
  <section class="wx-card wx-span-2" id="isolation-layers">
    <div class="wx-card-head"><h2>三层隔离</h2><span class="wx-badge">租户 → ACL → RLS</span></div>
    <div class="wx-layers" id="isolation-body"></div>
  </section>
  <section class="wx-card wx-span-2" id="permission-reason">
    <div class="wx-card-head"><h2>拒绝原因</h2><span class="wx-badge" id="permission-decision">-</span></div>
    <div id="permission-reason-body"></div>
  </section>
  <section class="wx-card wx-span-2" id="identity-compare">
    <div class="wx-card-head"><h2>身份切换对比</h2><span class="wx-badge">管理员可用</span></div>
    <div id="identity-compare-body" class="wx-empty">选择 query 后对比 guest / member / admin</div>
  </section>
</div>
```

**Step 4: 加加载与渲染 JS**

```javascript
// opsJson 已在 Task 8 定义，支持可选 options。

async function loadIdentity() {
  const identity = await opsJson("/v1/identity");
  const permissions = await opsJson(
    `/v1/ops/permissions?kb_id=${encodeURIComponent(currentScope().kb)}`
  );
  window.__identity = identity;
  renderIdentity(identity, permissions);
  if (typeof updateActionAvailability === "function") updateActionAvailability();
}

function renderIdentity(identity, permissions) {
  document.getElementById("identity-role").textContent = identity.role;
  document.getElementById("identity-role").classList.toggle(
    "is-real", identity.is_admin
  );
  document.getElementById("identity-body").innerHTML = [
    ["租户", identity.tenant],
    ["用户", identity.user],
    ["角色", identity.role],
    ["管理员", identity.is_admin ? "是" : "否"],
    ["可触发调测", identity.ops.can_trigger ? "是" : "否（管理员/开关限制）"],
  ].map(([key, value]) => `<div><span>${key}</span><strong>${value}</strong></div>`).join("");
  document.getElementById("identity-tags").innerHTML =
    identity.tags.map((tag) => `<span class="wx-tag">${tag}</span>`).join("") ||
    '<span class="wx-empty">无标签</span>';
  document.getElementById("identity-kb-count").textContent = `${identity.kbs.length} 个`;
  document.getElementById("identity-kb-body").innerHTML =
    identity.kbs.map((kb) => `<button class="wx-list-row" data-kb="${kb}">${kb}</button>`).join("");
  document.getElementById("isolation-body").innerHTML = [
    ["1. 租户作用域", identity.isolation.tenant_scope ? "启用" : "缺失", "缺 tenant 直接抛错"],
    ["2. 文档 ACL", permissions.acl.mode, `tags: ${permissions.acl.user_tags.join(", ") || "无"}`],
    ["3. 数据库 RLS", permissions.rls.enabled ? "启用" : "未启用", `bare_count=${permissions.rls.bare_count}`],
  ].map(([name, status, detail]) => `
    <div class="wx-layer">
      <div><strong>${name}</strong><span>${detail}</span></div>
      <span class="wx-badge ${status === "启用" ? "is-real" : ""}">${status}</span>
    </div>
  `).join("");
  document.getElementById("permission-decision").textContent =
    permissions.decision === "allow" ? "允许" : "拒绝";
  document.getElementById("permission-decision").classList.toggle(
    "is-danger", permissions.decision !== "allow"
  );
  document.getElementById("permission-reason-body").innerHTML = `
    <p>${permissions.reason}</p>
    <div class="wx-kv">
      <div><span>KB</span><strong>${permissions.kb.id}</strong></div>
      <div><span>租户匹配</span><strong>${permissions.kb.tenant_match ? "是" : "否"}</strong></div>
      <div><span>可见 chunk</span><strong>${permissions.rls.scoped_count}</strong></div>
      <div><span>RLS 裸连接</span><strong>${permissions.rls.bare_count}</strong></div>
    </div>
  `;
  document.querySelectorAll("[data-kb]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.kb.value = button.dataset.kb;
      updateScopePreview();
      loadIdentity();
    });
  });
}
```

在 `showView("identity")` 时调用 `loadIdentity()`；tenant/user/kb 变化时若当前在身份视图也重新加载。

**Step 5: 跑测试并手工验证**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

手工验证：切换 `u_admin`/`u_member`/`ghost`，身份卡、tags、可见 KB、三层隔离和拒绝原因同步变化。

**Step 6: Commit**

```bash
git add app/static/index.html tests/test_api_ui.py
git commit -m "feat(ui): add identity and permission panels"
```

---

## P2：受控动作与审计

### Task 10: ops_runs / ops_audit 状态机与审计 API

**Files:**
- Modify: `src/ops_runs.py`
- Create: `src/ops_audit.py`
- Modify: `app/deps.py`（`require_admin`）
- Modify: `app/routers/ops.py`
- Test: `tests/test_ops_runs.py`
- Test: `tests/test_ops_audit_api.py`

**Step 1: 写失败测试**

```python
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.db.base import create_db_engine
from src.ops_runs import (
    AlreadyRunning, claim_next, enqueue, get_run, list_runs,
    mark_failed, mark_succeeded,
)


def test_enqueue_rejects_same_active_kind(engine) -> None:
    run_id = enqueue(
        engine, TENANT, KB, "eval_mock", requested_by="u_admin", params={}
    )
    with pytest.raises(AlreadyRunning):
        enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin", params={})
    assert get_run(engine, TENANT, run_id)["status"] == "queued"


def test_claim_is_exclusive_and_finishes(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")
    claimed = claim_next(engine, kind="eval_mock")
    assert claimed["id"] == run_id
    assert claimed["status"] == "running"
    mark_succeeded(
        engine, run_id, exit_code=0,
        summary={"metrics": {"recall_at_5": 1.0}},
        artifact_path="data/ops/eval/1.json", output_tail="ok",
    )
    assert get_run(engine, TENANT, run_id)["status"] == "succeeded"


def test_failed_run_records_error(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "loadtest", requested_by="u_admin")
    claim_next(engine, kind="loadtest")
    mark_failed(engine, run_id, error="timeout", exit_code=None)
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "timeout"


def test_list_runs_is_tenant_scoped(engine) -> None:
    enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")
    assert list_runs(engine, "other", KB) == []
```

审计 API 测试：

```python
def test_audit_requires_admin_and_switch(client, engine) -> None:
    response = client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )
    assert response.status_code == 403
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_runs.py -q`

Expected: FAIL（`AlreadyRunning` 不存在）。

测试文件顶部加清理 fixture，避免 `claim_next` 领到其他用例残留的 queued 记录：

```python
import pytest
from sqlalchemy import text


@pytest.fixture(autouse=True)
def clean_ops(engine):
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM ops_runs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ops_audit WHERE tenant_id = :t"), {"t": TENANT}
        )
    yield
```

**Step 3: 扩展 ops_runs**

`src/ops_runs.py` 追加：

```python
import json

from sqlalchemy.exc import IntegrityError


class AlreadyRunning(Exception):
    def __init__(self, run_id: int):
        super().__init__(f"run already active: {run_id}")
        self.run_id = run_id


def enqueue(
    engine: Engine, tenant_id: str, kb_id: str, kind: str, *,
    requested_by: str, params: dict | None = None,
) -> int:
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "INSERT INTO ops_runs "
                    "(tenant_id, kb_id, kind, status, params, requested_by) "
                    "VALUES (:t, :kb, :kind, 'queued', CAST(:params AS jsonb), :by) "
                    "RETURNING id"
                ),
                {
                    "t": tenant_id, "kb": kb_id, "kind": kind,
                    "params": json.dumps(params or {}, ensure_ascii=False),
                    "by": requested_by,
                },
            ).scalar_one()
        return int(row)
    except IntegrityError:
        with engine.connect() as connection:
            existing = connection.execute(
                text(
                    "SELECT id FROM ops_runs "
                    "WHERE tenant_id = :t AND kb_id = :kb AND kind = :kind "
                    "AND status IN ('queued','running') ORDER BY id LIMIT 1"
                ),
                {"t": tenant_id, "kb": kb_id, "kind": kind},
            ).scalar_one()
        raise AlreadyRunning(int(existing))


def claim_next(engine: Engine, *, kind: str | None = None) -> dict | None:
    query = (
        "SELECT id, tenant_id, kb_id, kind, params, requested_by, "
        "'running' AS status "
        "FROM ops_runs WHERE status = 'queued'"
    )
    params: dict[str, object] = {}
    if kind:
        query += " AND kind = :kind"
        params["kind"] = kind
    query += " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
    with engine.begin() as connection:
        row = connection.execute(text(query), params).mappings().first()
        if row is None:
            return None
        connection.execute(
            text(
                "UPDATE ops_runs SET status='running', attempts=attempts+1, "
                "started_at=now() WHERE id=:id"
            ),
            {"id": row["id"]},
        )
    return dict(row)


def mark_succeeded(
    engine: Engine, run_id: int, *, exit_code: int, summary: dict,
    artifact_path: str | None, output_tail: str,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_runs SET status='succeeded', exit_code=:code, "
                "summary=CAST(:summary AS jsonb), artifact_path=:path, "
                "output_tail=:tail, finished_at=now(), error=NULL WHERE id=:id"
            ),
            {
                "id": run_id, "code": exit_code,
                "summary": json.dumps(summary, ensure_ascii=False),
                "path": artifact_path, "tail": output_tail[:8192],
            },
        )


def mark_failed(
    engine: Engine, run_id: int, *, error: str, exit_code: int | None,
    summary: dict | None = None, artifact_path: str | None = None,
    output_tail: str = "",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ops_runs SET status='failed', exit_code=:code, "
                "summary=CAST(:summary AS jsonb), artifact_path=:path, "
                "output_tail=:tail, error=:error, finished_at=now() WHERE id=:id"
            ),
            {
                "id": run_id, "code": exit_code,
                "summary": json.dumps(summary or {}, ensure_ascii=False),
                "path": artifact_path, "tail": output_tail[:8192],
                "error": error[:2000],
            },
        )
```

`src/ops_audit.py`：

```python
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine


def record(
    engine: Engine, *, tenant_id: str, actor: str, action: str,
    target: str | None, result: str, reason: str = "", detail: dict | None = None,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ops_audit "
                "(tenant_id, actor, action, target, result, reason, detail) "
                "VALUES (:t, :actor, :action, :target, :result, :reason, "
                "CAST(:detail AS jsonb))"
            ),
            {
                "t": tenant_id, "actor": actor, "action": action,
                "target": target, "result": result, "reason": reason,
                "detail": json.dumps(detail or {}, ensure_ascii=False),
            },
        )


def list_records(engine: Engine, tenant_id: str, *, limit: int = 50) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, tenant_id, actor, action, target, result, reason, "
                "detail, created_at FROM ops_audit "
                "WHERE tenant_id = :t ORDER BY id DESC LIMIT :limit"
            ),
            {"t": tenant_id, "limit": max(1, min(limit, 200))},
        ).mappings().all()
    return [dict(row) for row in rows]
```

**Step 4: 加管理员依赖和 API**

`app/deps.py`：

```python
def require_admin(engine: Engine, tenant_id: str, user_id: str) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT role, tags FROM users "
                "WHERE tenant_id = :t AND user_id = :u"
            ),
            {"t": tenant_id, "u": user_id},
        ).mappings().first()
    role = str(row["role"]) if row else "guest"
    if role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return {"role": role, "tags": list(row["tags"] or []) if row else []}
```

`app/routers/ops.py`：

```python
from src import ops_audit, ops_runs
from app.deps import ops_actions_enabled, require_admin


def _require_actions(engine, tenant_id: str, user_id: str, action: str, target: str | None):
    try:
        require_admin(engine, tenant_id, user_id)
        if not ops_actions_enabled():
            raise HTTPException(status_code=403, detail="ops actions disabled")
    except HTTPException as exc:
        ops_audit.record(
            engine, tenant_id=tenant_id, actor=user_id, action=action,
            target=target, result="denied", reason=str(exc.detail),
        )
        raise


@router.get("/ops/runs")
def list_runs_api(kb_id: str, tenant_id: TenantId, engine: EngineDep) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    return {"runs": ops_runs.list_runs(engine, tenant_id, kb_id)}


@router.get("/ops/runs/{run_id}")
def get_run_api(run_id: int, tenant_id: TenantId, engine: EngineDep) -> dict:
    run = ops_runs.get_run(engine, tenant_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.get("/ops/audit")
def audit_api(tenant_id: TenantId, user_id: UserId, engine: EngineDep, limit: int = 50) -> dict:
    _require_actions(engine, tenant_id, user_id, "audit", None)
    return {"audit": ops_audit.list_records(engine, tenant_id, limit=limit)}
```

**Step 5: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_ops_runs.py tests/test_ops_audit_api.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add src/ops_runs.py src/ops_audit.py app/deps.py app/routers/ops.py tests/test_ops_runs.py tests/test_ops_audit_api.py
git commit -m "feat(ops): add run state machine and audit API"
```

---

### Task 11: 独立 ops-runner 与白名单执行

**Files:**
- Create: `scripts/run_ops.py`
- Create: `tests/test_ops_runner.py`

**Step 1: 写失败测试**

```python
import json
import subprocess
from pathlib import Path

from src.db.base import create_db_engine
from src.ops_runs import enqueue, get_run
from scripts.run_ops import build_command, run_once


def test_build_command_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        build_command("rm -rf /", {"id": 1, "kb_id": "kb", "tenant_id": "t"})


def test_run_once_marks_eval_succeeded(engine, tmp_path) -> None:
    run_id = enqueue(engine, TENANT, KB, "eval_mock", requested_by="u_admin")

    def fake_runner(args, **kwargs):
        out = Path(args[args.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"metrics": {"recall_at_k": 1.0}, "counts": {"cases": 50}}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args, 0, "ok", "")

    assert run_once(engine, runner=fake_runner, kind="eval_mock") is True
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "succeeded"
    assert run["summary"]["metrics"]["recall_at_k"] == 1.0
    assert run["artifact_path"].startswith("data/ops/eval/")


def test_runner_timeout_marks_failed(engine) -> None:
    run_id = enqueue(engine, TENANT, KB, "loadtest", requested_by="u_admin")

    def timeout_runner(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    assert run_once(engine, runner=timeout_runner, kind="loadtest") is True
    run = get_run(engine, TENANT, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "timeout"
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_runner.py -q`

Expected: FAIL（`scripts.run_ops` 不存在）。

**Step 3: 实现 runner**

`scripts/run_ops.py`：

```python
#!/usr/bin/env python3
"""独立 ops-runner：白名单执行评测/压测/安全演练。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from src.db.base import create_db_engine
from src.ops_runs import claim_next, mark_failed, mark_succeeded

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 8192
TIMEOUTS = {
    "eval_mock": 300,
    "loadtest": 300,
    "drill_parser": 120,
    "drill_llm": 120,
    "drill_queue": 120,
}
DRILL_COMMANDS = {
    "drill_parser": [
        sys.executable, "-m", "pytest",
        "tests/test_ocr_branch.py", "tests/test_pdf_parser.py", "-q",
    ],
    "drill_llm": [sys.executable, "-m", "pytest", "tests/test_llm.py", "-q"],
    "drill_queue": [
        sys.executable, "-m", "pytest", "tests/test_ingest_jobs.py", "-q",
    ],
}


def build_command(kind: str, run: dict) -> tuple[list[str], str | None]:
    if kind == "eval_mock":
        artifact = f"data/ops/eval/{run['id']}.json"
        return [
            sys.executable, "scripts/run_eval.py", "--mock", "--out", artifact,
        ], artifact
    if kind == "loadtest":
        artifact = f"data/ops/loadtest/{run['id']}.json"
        return [
            sys.executable, "scripts/loadtest.py",
            "--concurrency", "20", "--requests", "200",
            "--kb", run["kb_id"], "--tenant", run["tenant_id"],
            "--out", artifact,
        ], artifact
    if kind in DRILL_COMMANDS:
        return list(DRILL_COMMANDS[kind]), None
    raise ValueError(f"unsupported ops kind: {kind}")


def _summary(kind: str, artifact: str | None, returncode: int) -> dict:
    if artifact and Path(ROOT / artifact).exists():
        data = json.loads((ROOT / artifact).read_text(encoding="utf-8"))
        if kind == "eval_mock":
            return {"metrics": data.get("metrics", {}), "counts": data.get("counts", {})}
        if kind == "loadtest":
            return data.get("summary", data)
    return {"passed": returncode == 0}


def run_once(engine, *, runner=subprocess.run, kind: str | None = None) -> bool:
    run = claim_next(engine, kind=kind)
    if run is None:
        return False

    kind = run["kind"]
    args, artifact = build_command(kind, run)
    try:
        completed = runner(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=TIMEOUTS[kind],
            shell=False,
        )
        tail = (completed.stdout or "")[-MAX_OUTPUT:]
        summary = _summary(kind, artifact, completed.returncode)
        if completed.returncode == 0:
            mark_succeeded(
                engine, run["id"], exit_code=completed.returncode,
                summary=summary, artifact_path=artifact, output_tail=tail,
            )
        else:
            mark_failed(
                engine, run["id"], error=f"exit_code={completed.returncode}",
                exit_code=completed.returncode, summary=summary,
                artifact_path=artifact, output_tail=tail,
            )
    except subprocess.TimeoutExpired as exc:
        mark_failed(
            engine, run["id"], error="timeout", exit_code=None,
            output_tail=str(exc)[:MAX_OUTPUT],
        )
    except Exception as exc:  # noqa: BLE001
        mark_failed(
            engine, run["id"], error=str(exc)[:2000], exit_code=None,
            output_tail="",
        )
    return True


def main() -> None:
    engine = create_db_engine()
    interval = float(os.environ.get("OPS_RUNNER_INTERVAL", "5"))
    while True:
        ran = run_once(engine)
        if not ran:
            time.sleep(interval)


if __name__ == "__main__":
    main()
```

**Step 4: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_ops_runner.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 5: Commit**

```bash
git add scripts/run_ops.py tests/test_ops_runner.py
git commit -m "feat(ops): add allowlisted ops runner"
```

---

### Task 12: 任务重试与通用运行入队 API

**Files:**
- Modify: `app/routers/ops.py`
- Modify: `src/ingest_jobs.py`（`retry_job` 已在 Task 2 实现，确认导出）
- Test: `tests/test_ops_actions_api.py`

**Step 1: 写失败测试**

```python
def test_non_admin_cannot_retry_job(client, engine) -> None:
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE ingest_jobs SET status='failed', error='boom' WHERE id=:id"),
            {"id": job_id},
        )
    response = client.post(
        f"/v1/ops/jobs/{job_id}/retry",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )
    assert response.status_code == 403
    audit = client.get(
        "/v1/ops/audit",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    ).json()["audit"]
    assert any(item["result"] == "denied" for item in audit)


def test_admin_retry_resets_failed_job(client, engine, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    job_id = enqueue(engine, TENANT, KB, doc_id="doc_retry_ok")
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE ingest_jobs SET status='failed', attempts=3, progress=80, "
                "error='boom', finished_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
    response = client.post(
        f"/v1/ops/jobs/{job_id}/retry",
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 202
    job = client.get(
        f"/v1/jobs/{job_id}", headers={"X-Tenant-Id": TENANT}
    ).json()
    assert job["status"] == "queued"
    assert job["attempts"] == 0
    assert job["progress"] == 0


def test_admin_can_enqueue_eval_run(client, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    response = client.post(
        f"/v1/ops/kbs/{KB}/runs",
        json={"kind": "eval_mock", "params": {}},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 202
    assert response.json()["run_id"] > 0


def test_duplicate_active_run_returns_409(client, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    headers = {"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"}
    body = {"kind": "eval_mock", "params": {}}
    assert client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=headers).status_code == 202
    assert client.post(f"/v1/ops/kbs/{KB}/runs", json=body, headers=headers).status_code == 409
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_ops_actions_api.py -q`

Expected: FAIL（路由 404）。

**Step 3: 实现动作 API**

`app/routers/ops.py`：

```python
from pydantic import BaseModel, Field

from src.ingest_events import record_job_event


class RunRequest(BaseModel):
    kind: str = Field(pattern="^(eval_mock|loadtest|drill_parser|drill_llm|drill_queue)$")
    params: dict = Field(default_factory=dict)


@router.post("/ops/kbs/{kb_id}/runs", status_code=202)
def enqueue_run(
    kb_id: str,
    payload: RunRequest,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    ensure_kb(engine, tenant_id, kb_id)
    _require_actions(engine, tenant_id, user_id, f"run_{payload.kind}", kb_id)
    try:
        run_id = ops_runs.enqueue(
            engine, tenant_id, kb_id, payload.kind,
            requested_by=user_id, params=payload.params,
        )
    except ops_runs.AlreadyRunning as exc:
        ops_audit.record(
            engine, tenant_id=tenant_id, actor=user_id,
            action=f"run_{payload.kind}", target=str(exc.run_id),
            result="denied", reason="already_running",
        )
        raise HTTPException(
            status_code=409,
            detail={"message": "run already active", "run_id": exc.run_id},
        )
    ops_audit.record(
        engine, tenant_id=tenant_id, actor=user_id,
        action=f"run_{payload.kind}", target=str(run_id),
        result="allowed", detail={"params": payload.params},
    )
    return {"run_id": run_id, "status": "queued"}


@router.post("/ops/jobs/{job_id}/retry", status_code=202)
def retry_job_api(
    job_id: int,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
) -> dict:
    _require_actions(engine, tenant_id, user_id, "job_retry", str(job_id))
    job = ingest_jobs.retry_job(engine, tenant_id, job_id)
    if job is None:
        existing = ingest_jobs.get_job(engine, tenant_id, job_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="job not found")
        raise HTTPException(status_code=409, detail="job is not failed")
    record_job_event(
        engine, job_id=job_id, tenant_id=tenant_id,
        stage="queued", status="started", progress=0,
        message="manual retry",
    )
    ops_audit.record(
        engine, tenant_id=tenant_id, actor=user_id,
        action="job_retry", target=str(job_id), result="allowed",
    )
    return {"job_id": job_id, "status": "queued"}
```

**Step 4: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_ops_actions_api.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 5: Commit**

```bash
git add app/routers/ops.py src/ingest_jobs.py tests/test_ops_actions_api.py
git commit -m "feat(ops): add job retry and run enqueue APIs"
```

---

### Task 13: 评测/压测运行页、触发按钮与审计时间线

**Files:**
- Modify: `app/static/index.html`
- Test: `tests/test_api_ui.py`（只追加）

**Step 1: 写失败测试**

```python
def test_ui_has_runs_and_audit_views(client) -> None:
    html = client.get("/ui/").text
    for marker in (
        'data-view="runs"',
        'data-view="audit"',
        'id="runs-metrics"',
        'id="runs-history"',
        'id="run-trigger-eval"',
        'id="run-trigger-loadtest"',
        'id="run-trigger-drill"',
        'id="audit-timeline"',
        'id="job-retry-button"',
    ):
        assert marker in html
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

Expected: FAIL。

**Step 3: 加运行页 HTML**

在 `section[data-view="runs"]` 中加：

```html
<div class="wx-grid">
  <section class="wx-card wx-span-2" id="runs-metrics">
    <div class="wx-card-head"><h2>最近一次评测</h2><span class="wx-badge" id="runs-metrics-badge">-</span></div>
    <div class="wx-metrics" id="runs-metrics-body"></div>
  </section>
  <section class="wx-card" id="runs-history">
    <div class="wx-card-head"><h2>运行历史</h2><span class="wx-badge" id="runs-history-badge">-</span></div>
    <div id="runs-history-body" class="wx-list-simple"></div>
  </section>
  <section class="wx-card" id="runs-trigger">
    <div class="wx-card-head"><h2>受控触发</h2><span class="wx-badge" id="runs-trigger-badge">只读</span></div>
    <div class="wx-actions">
      <button id="run-trigger-eval" class="wx-button" disabled>运行 mock 评测</button>
      <button id="run-trigger-loadtest" class="wx-button" disabled>运行固定压测</button>
      <button id="run-trigger-drill" class="wx-button" disabled>运行安全演练</button>
    </div>
    <p class="wx-hint">固定参数：eval --mock；loadtest 并发 20 / 每端点 200；演练为隔离测试。</p>
  </section>
  <section class="wx-card wx-span-2" id="runs-detail">
    <div class="wx-card-head"><h2>运行详情</h2><span class="wx-badge" id="runs-detail-badge">未选择</span></div>
    <div id="runs-detail-body" class="wx-empty">选择一条运行记录</div>
  </section>
</div>
```

在 `section[data-view="audit"]` 中加：

```html
<div class="wx-card">
  <div class="wx-card-head"><h2>操作审计</h2><span class="wx-badge" id="audit-badge">最近 50 条</span></div>
  <div id="audit-timeline" class="wx-audit"></div>
</div>
```

在任务看板卡片里加：

```html
<button id="job-retry-button" class="wx-button is-danger" hidden>重试失败任务</button>
```

**Step 4: 加载运行历史与触发**

```javascript
async function loadRuns() {
  const data = await opsJson(
    `/v1/ops/runs?kb_id=${encodeURIComponent(currentScope().kb)}&limit=20`
  );
  renderRuns(data.runs);
  updateActionAvailability();
}

function renderRuns(runs) {
  document.getElementById("runs-history-badge").textContent = `${runs.length} 次`;
  document.getElementById("runs-history-body").innerHTML =
    runs.map((run) => `
      <button class="wx-list-row" data-run-id="${run.id}">
        <span>#${run.id} ${run.kind}</span>
        <span class="wx-badge ${run.status === "failed" ? "is-danger" : "is-real"}">${run.status}</span>
        <span>${run.requested_by} · ${run.requested_at}</span>
      </button>
    `).join("") || '<div class="wx-empty">暂无运行记录</div>';
  document.querySelectorAll("[data-run-id]").forEach((button) => {
    button.addEventListener("click", () => showRun(Number(button.dataset.runId)));
  });
  const latestEval = runs.find((run) => run.kind === "eval_mock" && run.summary);
  if (latestEval) renderRunMetrics(latestEval);
}

async function triggerRun(kind) {
  const response = await checkedFetch(
    `/v1/ops/kbs/${encodeURIComponent(currentScope().kb)}/runs`,
    {
      method: "POST",
      headers: requestHeaders(currentScope(), true, true),
      body: JSON.stringify({ kind, params: {} }),
    }
  );
  const data = await response.json();
  await loadRuns();
  pollRun(data.run_id);
}

async function pollRun(runId) {
  const data = await opsJson(`/v1/ops/runs/${runId}`);
  showRun(data);
  if (data.status === "queued" || data.status === "running") {
    setTimeout(() => pollRun(runId), 2000);
  }
}

function updateActionAvailability() {
  const enabled = window.__identity?.ops?.can_trigger === true;
  for (const id of ["run-trigger-eval", "run-trigger-loadtest", "run-trigger-drill"]) {
    document.getElementById(id).disabled = !enabled;
  }
  document.getElementById("runs-trigger-badge").textContent = enabled ? "管理员可用" : "只读";
  document.getElementById("runs-trigger-badge").classList.toggle("is-real", enabled);
}
```

在 `loadIdentity()` 成功后设置 `window.__identity = identity` 并调用 `updateActionAvailability()`。

**Step 5: 加审计时间线**

```javascript
async function loadAudit() {
  const target = document.getElementById("audit-timeline");
  let data;
  try {
    data = await opsJson("/v1/ops/audit?limit=50");
  } catch (error) {
    // 非管理员或 OPS_ENABLE_ACTIONS 关闭时后端返回 403，这里降级为空态。
    target.innerHTML = `<div class="wx-empty">无审计权限：${escapeHtml(error.message)}</div>`;
    return;
  }
  target.innerHTML = data.audit.map((item) => `
    <div class="wx-audit-item ${item.result}">
      <div class="wx-audit-dot"></div>
      <div>
        <strong>${item.actor} · ${item.action}</strong>
        <p>${item.target || "-"} · ${item.result}${item.reason ? ` · ${item.reason}` : ""}</p>
        <time>${item.created_at}</time>
      </div>
    </div>
  `).join("") || '<div class="wx-empty">暂无审计记录</div>';
}
```

**Step 6: 运行详情、最近评测与任务重试**

```javascript
function showRun(run) {
  document.getElementById("runs-detail-badge").textContent = `#${run.id} · ${run.status}`;
  document.getElementById("runs-detail-body").innerHTML = `
    <div class="wx-kv">
      <div><span>类型</span><strong>${run.kind}</strong></div>
      <div><span>状态</span><strong>${run.status}</strong></div>
      <div><span>退出码</span><strong>${run.exit_code ?? "-"}</strong></div>
      <div><span>发起人</span><strong>${run.requested_by}</strong></div>
      <div><span>开始</span><strong>${run.started_at || "-"}</strong></div>
      <div><span>结束</span><strong>${run.finished_at || "-"}</strong></div>
      <div><span>产物</span><strong>${run.artifact_path || "-"}</strong></div>
    </div>
    ${run.error ? `<p class="wx-error">${escapeHtml(run.error)}</p>` : ""}
    <pre class="wx-output">${escapeHtml(run.output_tail || "无输出")}</pre>
  `;
}

function renderRunMetrics(run) {
  const metrics = run.summary?.metrics || {};
  document.getElementById("runs-metrics-badge").textContent = `#${run.id} ${run.status}`;
  document.getElementById("runs-metrics-body").innerHTML = [
    ["Recall@5", fmtNumber(metrics.recall_at_k)],
    ["MRR@5", fmtNumber(metrics.mrr)],
    ["Hit Rate@5", fmtNumber(metrics.hit_rate)],
    ["拒答准确率", fmtPercent(metrics.refusal_accuracy)],
    ["答案覆盖率", fmtPercent(metrics.answer_coverage)],
    ["引用准确率", fmtPercent(metrics.citation_accuracy)],
    ["用例数", run.summary?.counts?.cases ?? "-"],
    ["生成错误", run.summary?.counts?.generation_errors ?? 0],
  ].map(([label, value]) => `<div class="wx-metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

function updateRetryButton(job) {
  const allowed = window.__identity?.ops?.can_trigger === true;
  document.getElementById("job-retry-button").hidden = !(
    allowed && job && job.status === "failed"
  );
}

async function retrySelectedJob() {
  if (!opsState.selectedJobId) return;
  await checkedFetch(`/v1/ops/jobs/${opsState.selectedJobId}/retry`, {
    method: "POST",
    headers: requestHeaders(currentScope(), true),
  });
  await loadOpsJobs();
  await loadJobEvents(opsState.selectedJobId);
  await loadAudit();
}

document.getElementById("job-retry-button").addEventListener("click", retrySelectedJob);
```

再把前面代码里的两处衔接补上：

- `loadRuns()` 顶部加 `window.__runs = data.runs;`（供 Task 15 的趋势对比使用）。
- `loadJobEvents(jobId)` 末尾加：
  ```javascript
  updateRetryButton(opsState.jobs.find((item) => item.id === jobId));
  ```

**Step 7: 跑测试并手工验证**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

手工验证：

- 管理员 + `OPS_ENABLE_ACTIONS=1` 时按钮可用；非管理员/开关关闭时禁用。
- 触发 mock 评测后出现 queued → running → succeeded。
- 审计时间线出现 allowed/denied 记录。

**Step 8: Commit**

```bash
git add app/static/index.html tests/test_api_ui.py
git commit -m "feat(ui): add ops runs, triggers, and audit timeline"
```

---

### Task 14: 身份切换对比 API 与面板

**Files:**
- Modify: `app/routers/identity.py`
- Modify: `app/static/index.html`
- Test: `tests/test_identity_compare.py`

**Step 1: 写失败测试**

```python
def test_compare_requires_admin(client: TestClient) -> None:
    response = client.post(
        f"/v1/ops/kbs/{KB}/identity-compare",
        json={"query": "退款", "user_ids": ["guest", "u_member"]},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_member"},
    )
    assert response.status_code == 403


def test_compare_returns_per_user_hits(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("OPS_ENABLE_ACTIONS", "1")
    response = client.post(
        f"/v1/ops/kbs/{KB}/identity-compare",
        json={"query": "退款", "user_ids": ["guest", "u_member", "u_admin"], "top_k": 5},
        headers={"X-Tenant-Id": TENANT, "X-User-Id": "u_admin"},
    )
    assert response.status_code == 200
    rows = response.json()["users"]
    assert [row["user"] for row in rows] == ["guest", "u_member", "u_admin"]
    assert all("hits" in row and "latency_ms" in row for row in rows)
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_identity_compare.py -q`

Expected: FAIL（路由 404）。

**Step 3: 实现对比接口**

`app/routers/identity.py`：

```python
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.deps import EmbedderDep, ops_actions_enabled, require_admin
from app.deps import user_context_of
from app.services import build_retriever
from src import ops_audit


def _require_admin_and_switch(
    engine, tenant_id: str, user_id: str, action: str, target: str
) -> None:
    try:
        require_admin(engine, tenant_id, user_id)
        if not ops_actions_enabled():
            raise HTTPException(status_code=403, detail="ops actions disabled")
    except HTTPException as exc:
        ops_audit.record(
            engine, tenant_id=tenant_id, actor=user_id, action=action,
            target=target, result="denied", reason=str(exc.detail),
        )
        raise


class CompareRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    user_ids: list[str] = Field(min_length=1, max_length=3)
    top_k: int = Field(default=5, ge=1, le=10)


@router.post("/ops/kbs/{kb_id}/identity-compare")
def identity_compare(
    kb_id: str,
    payload: CompareRequest,
    tenant_id: TenantId,
    user_id: UserId,
    engine: EngineDep,
    embed_client: EmbedderDep,
) -> dict:
    _require_admin_and_switch(engine, tenant_id, user_id, "identity_compare", kb_id)
    ensure_kb(engine, tenant_id, kb_id)
    retriever = build_retriever(engine, tenant_id, kb_id, embed_client, need_bm25=True)
    rows = []
    for candidate in payload.user_ids:
        tags, is_admin = user_context_of(engine, tenant_id, candidate)
        user = _user_row(engine, tenant_id, candidate)
        started = time.perf_counter()
        hits = retriever.retrieve(
            payload.query,
            top_k=payload.top_k,
            filters={"audience": None, "user_tags": tags, "is_admin": is_admin},
            mode="hybrid",
        )
        rows.append({
            "user": candidate,
            "role": str(user.get("role") or "guest"),
            "tags": tags,
            "is_admin": is_admin,
            "hits": len(hits),
            "top_score": round(float(hits[0].score), 6) if hits else None,
            "visible_chunk_ids": [hit.chunk_id for hit in hits],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })
    return {"query": payload.query, "users": rows}
```

`_require_admin_and_switch` 复用 Task 10 的 `require_admin` + `ops_actions_enabled`；拒绝时写审计。

**Step 4: 加对比面板 JS**

```javascript
async function compareIdentity() {
  const query = document.getElementById("search-query").value.trim();
  if (!query) return;
  const data = await opsJson(
    `/v1/ops/kbs/${encodeURIComponent(currentScope().kb)}/identity-compare`,
    {
      method: "POST",
      headers: requestHeaders(currentScope(), true, true),
      body: JSON.stringify({
        query,
        user_ids: ["guest", "u_member", "u_admin"],
        top_k: 5,
      }),
    }
  );
  document.getElementById("identity-compare-body").innerHTML = `
    <table class="wx-table">
      <thead><tr><th>用户</th><th>角色</th><th>标签</th><th>命中</th><th>最高分</th><th>耗时</th></tr></thead>
      <tbody>${data.users.map((row) => `
        <tr>
          <td>${row.user}</td><td>${row.role}</td><td>${row.tags.join(", ") || "-"}</td>
          <td>${row.hits}</td><td>${row.top_score ?? "-"}</td><td>${row.latency_ms} ms</td>
        </tr>`).join("")}</tbody>
    </table>`;
}
```

在 Task 9 的 `identity-compare` 卡片里、`identity-compare-body` 之前加按钮，并绑定事件：

```html
<button id="identity-compare-button" class="wx-button" disabled>对比当前 query</button>
```

```javascript
document.getElementById("identity-compare-button").addEventListener("click", compareIdentity);
```

`updateActionAvailability()` 里同时设置 `document.getElementById("identity-compare-button").disabled = !enabled;`。`opsJson` 已在 Task 8 支持 `options` 参数。

**Step 5: 跑测试**

Run:

```bash
py -3.13 -m pytest tests/test_identity_compare.py -q
py -3.13 -m pytest -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add app/routers/identity.py app/static/index.html tests/test_identity_compare.py
git commit -m "feat(ops): add identity comparison API and panel"
```

---

## P3：压测、演练与最终验收

### Task 15: 固定压测/安全演练按钮、趋势对比与最终验收

**Files:**
- Modify: `app/static/index.html`
- Create: `docs/ops-cockpit-checklist.md`
- Create: `docs/adr/0009-ops-cockpit.md`
- Modify: `README.md`
- Test: `tests/test_api_ui.py`（只追加）

**Step 1: 写失败测试**

```python
def test_ui_has_run_metrics_and_delta(client) -> None:
    html = client.get("/ui/").text
    for marker in (
        'id="runs-delta"',
        'id="runs-detail-body"',
        'id="run-trigger-loadtest"',
        'id="run-trigger-drill"',
        'data-drill="drill_parser"',
        'data-drill="drill_llm"',
        'data-drill="drill_queue"',
    ):
        assert marker in html
```

**Step 2: 跑测试确认失败**

Run: `py -3.13 -m pytest tests/test_api_ui.py -q`

Expected: FAIL。

**Step 3: 绑定触发按钮与详情**

```javascript
document.getElementById("run-trigger-eval").addEventListener("click", () => triggerRun("eval_mock"));
document.getElementById("run-trigger-loadtest").addEventListener("click", () => {
  if (confirm("固定压测：并发 20，每端点 200 请求，会占用本机资源。继续？")) {
    triggerRun("loadtest");
  }
});
document.getElementById("run-trigger-drill").addEventListener("click", () => {
  document.getElementById("drill-actions").hidden = false;
});
document.querySelectorAll("[data-drill]").forEach((button) => {
  button.addEventListener("click", () => {
    if (confirm(`安全演练：${button.dataset.drill}，只运行隔离测试，不影响运行中的服务。继续？`)) {
      triggerRun(button.dataset.drill);
    }
  });
});

function renderDelta(run) {
  const previous = (window.__runs || []).find(
    (item) => item.kind === run.kind && item.id < run.id && item.summary
  );
  if (!previous) {
    document.getElementById("runs-delta").textContent = "首次运行，暂无对比";
    return;
  }
  const current = run.summary?.metrics || {};
  const before = previous.summary?.metrics || {};
  const rows = [
    ["Recall@5", "recall_at_k"],
    ["MRR@5", "mrr"],
    ["Hit Rate@5", "hit_rate"],
  ].map(([label, key]) => {
    const delta = Number(current[key] || 0) - Number(before[key] || 0);
    return `<div class="wx-delta ${delta < 0 ? "is-danger" : "is-real"}">
      <span>${label}</span><strong>${delta >= 0 ? "+" : ""}${delta.toFixed(4)}</strong>
    </div>`;
  });
  document.getElementById("runs-delta").innerHTML = rows.join("");
}
```

在 Task 13 的 `renderRunMetrics` 末尾追加一行 `renderDelta(run);`（函数本体已在 Task 13 定义，这里只新增对比渲染与按钮绑定）。

**Step 4: 在运行页加演练按钮**

在 `runs-trigger` 卡片里追加：

```html
<div id="drill-actions" class="wx-actions" hidden>
  <button data-drill="drill_parser" class="wx-button" disabled>parser 降级演练</button>
  <button data-drill="drill_llm" class="wx-button" disabled>LLM 失败演练</button>
  <button data-drill="drill_queue" class="wx-button" disabled>队列重试演练</button>
</div>
<div id="runs-delta" class="wx-delta-list"></div>
```

把 Task 13 的 `updateActionAvailability()` 整段替换为同时管理演练按钮的版本：

```javascript
function updateActionAvailability() {
  const enabled = window.__identity?.ops?.can_trigger === true;
  for (const id of ["run-trigger-eval", "run-trigger-loadtest", "run-trigger-drill"]) {
    document.getElementById(id).disabled = !enabled;
  }
  for (const button of document.querySelectorAll("[data-drill]")) {
    button.disabled = !enabled;
  }
  const compare = document.getElementById("identity-compare-button");
  if (compare) compare.disabled = !enabled;
  const badge = document.getElementById("runs-trigger-badge");
  badge.textContent = enabled ? "管理员可用" : "只读";
  badge.classList.toggle("is-real", enabled);
}
```

`run-trigger-drill` 只负责展开 `drill-actions`，真正触发的是三个 `[data-drill]` 按钮。

**Step 5: 写文档**

`docs/ops-cockpit-checklist.md` 至少包含：

```markdown
# 调测驾驶舱验收清单

## 环境
- commit：
- 运行模式：mock / real
- OPS_ENABLE_ACTIONS：0 / 1
- tenant / kb / user：

## 功能
- [ ] 三栏微信桌面版 shell，左导航选中态正常
- [ ] 驾驶舱显示健康、队列、任务时间线、AI 指标、Trace 瀑布
- [ ] 身份卡显示 role/tags/is_admin/可见 KB
- [ ] guest/member/admin 对比结果可见
- [ ] 拒绝原因能定位租户/ACL/RLS
- [ ] 管理员触发 mock 评测并看到指标
- [ ] 固定压测出现 p50/p95/p99
- [ ] 三项安全演练可运行
- [ ] 非管理员/开关关闭时按钮禁用
- [ ] 审计时间线包含 allowed/denied
- [ ] 900px 以下布局正常
- [ ] 键盘可遍历导航和按钮

## 证据
- 截图：
- 测试命令与结果：
```

`docs/adr/0009-ops-cockpit.md` 按既有五段格式写：状态、背景、决策、后果、对应代码。明确：

- 三栏 shell 与微信桌面版 token；
- ops-runner 白名单，不执行任意命令；
- 不真实停容器；
- 新表不新增 RLS 的边界；
- 不修改 `src/pipeline.py` 的包装器方案。

`README.md` 增加“调测驾驶舱”小节：启动 API/worker/ops-runner、`OPS_ENABLE_ACTIONS=1`、URL `/ui`、四项功能与指标口径。

**Step 6: 最终验证**

Run:

```bash
py -3.13 -m pytest -q
py -3.13 scripts/demo_acceptance.py
py -3.13 scripts/demo_ingest.py --mock
py -3.13 scripts/loadtest.py --concurrency 20 --requests 200 --out data/ops/loadtest/manual.json
```

Expected:

- 全量 pytest 通过；
- Gate C 6/6；
- demo_ingest PASS；
- loadtest 双端点 200/200，错误率 0；
- UI 手工清单全部勾选。

手工验收步骤：

1. 启动 `py -3.13 scripts/run_worker.py` 和 `py -3.13 scripts/run_ops.py`。
2. 打开 `/ui`，切换到驾驶舱，确认队列、AI 指标、Trace。
3. 切换 `u_member`/`u_admin`，确认身份卡和权限解释变化。
4. 用同一个 query 做身份对比，确认命中差异。
5. 管理员开启 `OPS_ENABLE_ACTIONS=1`，触发 mock 评测；确认 queued→running→succeeded。
6. 触发固定压测和三项演练；确认结果与审计。
7. 关闭开关/切换普通成员，确认按钮禁用。
8. 截图归档到 `docs/images/ops-*.jpg`，在清单中引用。

**Step 7: Commit**

```bash
git add app/static/index.html tests/test_api_ui.py docs/ops-cockpit-checklist.md docs/adr/0009-ops-cockpit.md README.md
git commit -m "docs: complete ops cockpit acceptance and ADR"
```

---

## 完成标准

- P1：任务事件、身份/权限、驾驶舱只读、Trace 列表可用。
- P2：管理员可重试任务、触发 mock 评测；审计与运行历史可用。
- P3：固定压测、三项安全演练、趋势对比和最终验收完成。
- 全量 pytest、Gate C、demo_ingest、loadtest、UI 手工清单全部通过。
- 不修改 spec 11.1 的 dirty/untracked 文件；不覆盖 `eval/baseline*.json`。

