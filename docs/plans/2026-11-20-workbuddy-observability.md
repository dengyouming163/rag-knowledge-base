# WorkBuddy 专业工作台与完整可观测性 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 `/ui` 重构为白色 WorkBuddy 专业工作台，并以 Postgres 请求指标支撑流量、延迟、质量、Token、成本、趋势和 Trace Inspector。

**Architecture:** 新增 `ops_http_metrics` 与 `ops_rag_metrics` 两张原始指标表；纯 ASGI 中间件记录完整 HTTP/SSE 响应，Search/Chat 在终态只写一行 RAG 指标，报表直接在有界时间窗内做 SQL 聚合。前端先以 `/ui/workbench.html` 并行构建，使用原生 ES modules、无构建步骤，全部能力就绪后再替换 `/ui/index.html`，避免中间提交破坏旧页面。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / Alembic / Postgres 16 / pytest / 原生 HTML/CSS/ES modules / Node 内置测试 / 内联 SVG。

**Spec:** `docs/superpowers/specs/2026-11-20-workbuddy-observability-design.md`

**执行约定：**

- 所有命令在 `projects/rag-knowledge-base` 下执行，Python 使用 `py -3.13`。
- 每个 Task：先写失败测试 → 验证 RED → 最小实现 → 验证 GREEN → 全量 `py -3.13 -m pytest -q` → 涉及前端时再跑 `node --test tests/js/*.test.mjs` → 只按文件名暂存并 commit。
- 表结构只通过 Alembic；禁止手写生产 DDL。迁移验证使用 `py -3.13 -m alembic upgrade head`。
- Tenant 只从 `X-Tenant-Id`；body 不接受 `tenant_id`；跨租户资源返回 404。
- 不修改、不暂存、不提交以下他人 dirty/untracked 文件：`.env.example`、`deploy/docker-compose.yml`、`eval/*`、`scripts/calibrate_refusal.py`、`scripts/run_eval.py`、`scripts/make_pdf_fixtures.py`、`src/__init__.py`、`src/pipeline.py`、`src/retrieve.py`、`src/refusal.py`、`src/parsers/`、`tests/test_api_search.py`、`tests/test_pipeline.py`、`tests/test_eval_generate.py`、`tests/test_pdf_parser.py`。
- 不覆盖 `eval/baseline*.json`；产物只写 `data/ops/`。不 amend、不 push；commit message 用英文。
- 新页面完成前不得删除旧 `/ui/index.html`；Task 16 是唯一允许切换入口的任务。
- 每个 Task 完成后按中文五段格式汇报：定位、目的、做了什么、完成了什么、术语。

---

## P0：指标存储与采集

### Task 1: 迁移 0011 与 ORM 模型

**Files:**
- Create: `migrations/versions/0011_ops_observability.py`
- Modify: `src/db/models.py`
- Modify: `tests/test_db_models.py`
- Create: `tests/test_ops_observability_migration.py`

**Step 1: 写失败测试**

```python
# tests/test_ops_observability_migration.py
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


def test_observability_tables_exist(engine) -> None:
    assert {"ops_http_metrics", "ops_rag_metrics"} <= set(
        inspect(engine).get_table_names()
    )


def test_rag_metric_constraints_and_indexes(engine) -> None:
    inspector = inspect(engine)
    columns = {item["name"] for item in inspector.get_columns("ops_rag_metrics")}
    assert {
        "trace_id", "operation", "outcome", "total_ms", "stage_spans",
        "rewrite_ms", "retrieval_ms", "rerank_ms", "llm_ms",
    } <= columns
    uniques = inspector.get_unique_constraints("ops_rag_metrics")
    assert any(
        item["name"] == "uq_ops_rag_metrics_tenant_trace"
        and item["column_names"] == ["tenant_id", "trace_id"]
        for item in uniques
    )


def test_metric_tables_have_no_foreign_keys(engine) -> None:
    inspector = inspect(engine)
    assert inspector.get_foreign_keys("ops_http_metrics") == []
    assert inspector.get_foreign_keys("ops_rag_metrics") == []
```

同时在 `tests/test_db_models.py::EXPECTED_TABLES` 追加 `ops_http_metrics`、`ops_rag_metrics`。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_observability_migration.py tests/test_db_models.py -q`

Expected: FAIL，两张表不存在，ORM 期望表集合也不一致。

**Step 3: 写迁移**

```python
"""HTTP/RAG 原始指标。Revision ID: 0011, Revises: 0010."""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_http_metrics (
            id bigserial PRIMARY KEY,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            tenant_id text NOT NULL,
            kb_id text,
            route text NOT NULL,
            method text NOT NULL,
            status_code smallint NOT NULL CHECK (status_code BETWEEN 100 AND 599),
            duration_ms double precision NOT NULL CHECK (duration_ms >= 0),
            trace_id text
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_http_metrics_tenant_time "
        "ON ops_http_metrics (tenant_id, occurred_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_http_metrics_tenant_kb_time "
        "ON ops_http_metrics (tenant_id, kb_id, occurred_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_rag_metrics (
            id bigserial PRIMARY KEY,
            trace_id text NOT NULL,
            tenant_id text NOT NULL,
            kb_id text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            operation text NOT NULL CHECK (operation IN ('search', 'chat')),
            mode text NOT NULL,
            outcome text NOT NULL CHECK (
                outcome IN ('succeeded', 'refused', 'error', 'cancelled')
            ),
            hits integer NOT NULL CHECK (hits >= 0),
            citation_ok boolean,
            rerank_degraded boolean,
            total_ms double precision NOT NULL CHECK (total_ms >= 0),
            rewrite_ms double precision CHECK (rewrite_ms >= 0),
            retrieval_ms double precision CHECK (retrieval_ms >= 0),
            rerank_ms double precision CHECK (rerank_ms >= 0),
            llm_ms double precision CHECK (llm_ms >= 0),
            model text,
            prompt_tokens integer CHECK (prompt_tokens >= 0),
            completion_tokens integer CHECK (completion_tokens >= 0),
            error_kind text,
            stage_spans jsonb NOT NULL DEFAULT '[]'::jsonb,
            CONSTRAINT uq_ops_rag_metrics_tenant_trace UNIQUE (tenant_id, trace_id),
            CONSTRAINT ck_ops_rag_metrics_spans_array
                CHECK (jsonb_typeof(stage_spans) = 'array')
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ops_rag_metrics_tenant_kb_time "
        "ON ops_rag_metrics (tenant_id, kb_id, occurred_at DESC, id DESC)"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE "
        "ops_http_metrics_id_seq, ops_rag_metrics_id_seq TO kb_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO kb_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ops_rag_metrics")
    op.execute("DROP TABLE IF EXISTS ops_http_metrics")
```

**Step 4: 加 ORM 模型**

在 `src/db/models.py` 末尾新增 `OpsHttpMetric`、`OpsRagMetric`，字段与迁移一一对应。关键定义：

```python
class OpsRagMetric(Base):
    __tablename__ = "ops_rag_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    kb_id: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_ok: Mapped[bool | None] = mapped_column()
    rerank_degraded: Mapped[bool | None] = mapped_column()
    total_ms: Mapped[float] = mapped_column(nullable=False)
    rewrite_ms: Mapped[float | None] = mapped_column()
    retrieval_ms: Mapped[float | None] = mapped_column()
    rerank_ms: Mapped[float | None] = mapped_column()
    llm_ms: Mapped[float | None] = mapped_column()
    model: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    error_kind: Mapped[str | None] = mapped_column(Text)
    stage_spans: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "trace_id", name="uq_ops_rag_metrics_tenant_trace"
        ),
        Index("ix_ops_rag_metrics_tenant_kb_time", "tenant_id", "kb_id", "occurred_at", "id"),
    )
```

`OpsHttpMetric` 同样不得声明 ForeignKey；其索引名为 `ix_ops_http_metrics_tenant_time`、`ix_ops_http_metrics_tenant_kb_time`。

**Step 5: 迁移并验证 GREEN**

Run: `py -3.13 -m alembic upgrade head`

Run: `py -3.13 -m pytest tests/test_ops_observability_migration.py tests/test_db_models.py -q`

Expected: PASS。再依次执行 `py -3.13 -m alembic downgrade 0010` 和 `py -3.13 -m alembic upgrade head`，确认可逆；PowerShell 使用两条独立命令。

**Step 6: 全量回归并提交**

Run: `py -3.13 -m pytest -q`

```bash
git add migrations/versions/0011_ops_observability.py src/db/models.py tests/test_db_models.py tests/test_ops_observability_migration.py
git commit -m "feat(ops): add observability metric tables"
```

---

### Task 2: 指标写入、校验与幂等

**Files:**
- Create: `src/ops_observability.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_ops_observability_store.py`

**Step 1: 写可复用 DB fixture 与失败测试**

在 `tests/conftest.py` 增加 `metric_scope`，每次使用随机后缀创建 Tenant/KB，并在前后删除两张指标表及作用域数据：

```python
@pytest.fixture()
def metric_scope():
    from uuid import uuid4
    from sqlalchemy import text
    from src.db.base import create_db_engine

    engine = create_db_engine()
    suffix = uuid4().hex[:10]
    tenant, kb = f"t_metric_{suffix}", f"kb_metric_{suffix}"
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            connection.execute(text("INSERT INTO tenants (id, name) VALUES (:t, 'metrics')"), {"t": tenant})
            connection.execute(text("INSERT INTO knowledge_bases (id, tenant_id, name, description, status) VALUES (:kb, :t, 'metrics', '', 'active')"), {"kb": kb, "t": tenant})
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        pytest.skip(f"数据库不可用：{exc}")
    yield engine, tenant, kb
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM ops_http_metrics WHERE tenant_id = :t"), {"t": tenant})
        connection.execute(text("DELETE FROM ops_rag_metrics WHERE tenant_id = :t"), {"t": tenant})
        connection.execute(text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": kb})
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
    engine.dispose()
```

测试必须覆盖：合法写入、相同 `(tenant_id, trace_id)` 第二次写入被忽略、非法 outcome、NaN/Infinity、超过 16 个 spans、未知阶段、负耗时、best-effort 写库失败不抛出。

```python
def valid_rag_fields(tenant: str, kb: str) -> dict:
    return {
        "trace_id": "tr_metric_test",
        "tenant_id": tenant,
        "kb_id": kb,
        "operation": "search",
        "mode": "hybrid",
        "outcome": "succeeded",
        "hits": 1,
        "total_ms": 10.0,
        "stage_spans": [
            {"stage": "retrieve", "start_ms": 0.0, "duration_ms": 10.0, "status": "ok"}
        ],
    }


def test_validate_stage_spans_rejects_non_finite() -> None:
    with pytest.raises(ValueError, match="finite"):
        validate_stage_spans([
            {"stage": "retrieve", "start_ms": 0, "duration_ms": float("nan"), "status": "ok"}
        ])


def test_rag_metric_insert_is_idempotent(metric_scope) -> None:
    engine, tenant, kb = metric_scope
    fields = valid_rag_fields(tenant, kb)
    record_rag_metric(engine, **fields)
    record_rag_metric(engine, **fields)
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM ops_rag_metrics WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    assert count == 1
```

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_observability_store.py -q`

Expected: ImportError，模块和函数不存在。

**Step 3: 实现校验与写入**

`src/ops_observability.py` 的公共契约：

```python
ALLOWED_STAGES = {"rewrite", "retrieve", "rerank", "answer", "other"}
ALLOWED_SPAN_STATUS = {"ok", "error", "cancelled"}
ALLOWED_OUTCOMES = {"succeeded", "refused", "error", "cancelled"}


def validate_stage_spans(spans: list[dict]) -> list[dict]:
    if not isinstance(spans, list) or len(spans) > 16:
        raise ValueError("stage_spans must be a list with at most 16 items")
    validated = []
    for span in spans:
        if set(span) != {"stage", "start_ms", "duration_ms", "status"}:
            raise ValueError("invalid stage span keys")
        if span["stage"] not in ALLOWED_STAGES or span["status"] not in ALLOWED_SPAN_STATUS:
            raise ValueError("invalid stage or status")
        start, duration = float(span["start_ms"]), float(span["duration_ms"])
        if not all(math.isfinite(value) and value >= 0 for value in (start, duration)):
            raise ValueError("stage span values must be finite and non-negative")
        validated.append({
            "stage": span["stage"], "start_ms": round(start, 3),
            "duration_ms": round(duration, 3), "status": span["status"],
        })
    return validated
```

必须实现 `record_http_metric(engine, **fields)`、`record_rag_metric(engine, **fields)`、`try_record_http_metric(engine, **fields)`、`try_record_rag_metric(engine, **fields)`，所有字段使用绑定参数。函数体不得保留省略号、`pass` 或 `NotImplementedError`。

要求：

- `record_rag_metric` 先校验 `operation/outcome/hits/total_ms/spans`，再执行完整 `INSERT`。
- `INSERT` 末尾必须是 `ON CONFLICT (tenant_id, trace_id) DO NOTHING`。
- 可选数值只有 `None` 或有限非负数；Token 必须是 `None` 或非负整数。
- `route/mode/model/error_kind` 分别限制 256/64/256/128 字符，超限拒绝而不是静默截断指标。
- `try_*` 捕获 `Exception` 并 `logger.exception(...)`，不得向业务路由抛出。
- 不用生产 DDL，不用 ORM `create_all()`。

**Step 4: 验证 GREEN 与全量回归**

Run: `py -3.13 -m pytest tests/test_ops_observability_store.py -q`

Run: `py -3.13 -m pytest -q`

**Step 5: 提交**

```bash
git add src/ops_observability.py tests/conftest.py tests/test_ops_observability_store.py
git commit -m "feat(ops): add validated metric writers"
```

---

### Task 3: 纯 ASGI HTTP 指标中间件

**Files:**
- Create: `app/http_metrics.py`
- Modify: `app/main.py`
- Create: `tests/test_http_metrics_middleware.py`

**Step 1: 写失败测试**

用独立 FastAPI app，不依赖真实数据库；monkeypatch `app.http_metrics._write` 收集字段。

```python
def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpMetricsMiddleware)

    @app.post("/v1/kbs/{kb_id}/probe")
    async def probe(kb_id: str):
        return {"ok": True}

    @app.get("/v1/ops/summary")
    async def summary():
        return {"ok": True}

    @app.get("/v1/stream")
    async def stream():
        async def body():
            yield b"one"
            await asyncio.sleep(0.01)
            yield b"two"
        return StreamingResponse(body())
    return app


def test_http_metric_uses_route_template_and_kb(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(http_metrics, "_write", lambda fields: calls.append(fields))
    client = TestClient(make_app())
    response = client.post("/v1/kbs/kb_a/probe", headers={"X-Tenant-Id": "t"})
    assert response.status_code == 200
    assert calls[0]["route"] == "/v1/kbs/{kb_id}/probe"
    assert calls[0]["kb_id"] == "kb_a"
    assert calls[0]["status_code"] == 200


def test_http_metric_waits_for_sse_and_skips_ops(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(http_metrics, "_write", lambda fields: calls.append(fields))
    client = TestClient(make_app())
    assert client.get("/v1/stream", headers={"X-Tenant-Id": "t"}).text == "onetwo"
    assert calls[0]["duration_ms"] >= 8
    client.get("/v1/ops/summary", headers={"X-Tenant-Id": "t"})
    assert len(calls) == 1
```

另测：缺少 `X-Tenant-Id` 不写；4xx/5xx 记录实际状态；recorder 抛异常时响应不变。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_http_metrics_middleware.py -q`

Expected: ImportError。

**Step 3: 实现中间件**

```python
class HttpMetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _instrumentable(scope):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500
        try:
            async def observed_send(message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = int(message["status"])
                await send(message)
            await self.app(scope, receive, observed_send)
        finally:
            try:
                fields = _fields_from_scope(
                    scope, status_code, (time.perf_counter() - started) * 1000
                )
                await asyncio.shield(run_in_threadpool(_write, fields))
            except Exception:
                logger.exception("failed to record HTTP metric")
```

模块级定义 `logger = logging.getLogger(__name__)`。采集或字段构造失败只能记录日志，不得替换、中断或重新抛出业务响应异常。

`_instrumentable` 只接受有非空 `X-Tenant-Id` 的 `/v1/` 请求，并排除 `/v1/ops/`。`_fields_from_scope`：

- Tenant 只读 header。
- `route` 在下游路由完成后取 `scope["route"].path`，没有则为 `unmatched`。
- `kb_id` 取 `scope["path_params"].get("kb_id")`，再取 `scope["state"].get("kb_id")`，最后为 `None`。
- `trace_id` 从 state 获取，没有则为 `None`。
- `_write(fields)` 内调用 `try_record_http_metric(get_engine(), **fields)`；测试替换 `_write`，不触发 DB。

在 `create_app()` 注册 `app.add_middleware(HttpMetricsMiddleware)`，保持现有异常处理与 StaticFiles 顺序。

**Step 4: 验证 GREEN 与全量回归**

Run: `py -3.13 -m pytest tests/test_http_metrics_middleware.py -q`

Run: `py -3.13 -m pytest -q`

**Step 5: 提交**

```bash
git add app/http_metrics.py app/main.py tests/test_http_metrics_middleware.py
git commit -m "feat(ops): record full HTTP and SSE metrics"
```

---

### Task 4: Search 终态指标

**Files:**
- Modify: `app/routers/search.py`
- Modify: `app/observability.py`
- Create: `tests/test_search_observability.py`

**Step 1: 写失败测试**

不得修改 dirty 的 `tests/test_api_search.py`。新建测试并直接调用路由，替换 `ensure_kb`、`user_context_of`、`build_retriever` 和 `try_record_rag_metric`：

- 成功：`operation="search"`、`outcome="succeeded"`、hits 正确、Token/LLM/Citation 为 `None`。
- 异常：原异常继续抛出，只写一次 `outcome="error"`，`error_kind` 为异常类名。
- `request.state.kb_id` 与 `request.state.trace_id` 均设置。
- recorder 抛异常已由 `try_record_rag_metric` 隔离，不改变 `SearchResponse`。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_search_observability.py -q`

Expected: FAIL，没有指标调用。

**Step 3: 最小实现**

给 `TimedRetriever` 增加可选 `origin` 和 `last_span`：

```python
class TimedRetriever:
    def __init__(self, inner: Any, *, origin: float | None = None) -> None:
        self.inner = inner
        self.origin = origin
        self.last_ms = 0.0
        self.last_span = None

    def retrieve(self, *args, **kwargs):
        started = time.perf_counter()
        status = "ok"
        try:
            return self.inner.retrieve(*args, **kwargs)
        except Exception:
            status = "error"
            raise
        finally:
            finished = time.perf_counter()
            self.last_ms = (finished - started) * 1000
            self.last_span = {
                "stage": "retrieve",
                "start_ms": 0.0 if self.origin is None else (started - self.origin) * 1000,
                "duration_ms": self.last_ms,
                "status": status,
            }
```

Search 使用 `origin = time.perf_counter()`，将 retriever 包装后执行；成功路径在现有 `trace_store.record` 后调用：

```python
try_record_rag_metric(
    engine,
    trace_id=trace_id,
    tenant_id=tenant_id,
    kb_id=kb_id,
    operation="search",
    mode=payload.mode,
    outcome="succeeded",
    hits=len(hits),
    citation_ok=None,
    rerank_degraded=None,
    total_ms=elapsed_ms,
    retrieval_ms=timed.last_ms,
    stage_spans=[timed.last_span],
)
```

异常路径写 `outcome="error"`、`hits=0`、`error_kind=exc.__class__.__name__`，随后 `raise`。所有未提供的关键字由 writer 默认成 `None`；不得传 Token=0。

**Step 4: 验证 GREEN 与全量回归**

Run: `py -3.13 -m pytest tests/test_search_observability.py tests/test_chat_usage.py -q`

Run: `py -3.13 -m pytest -q`

**Step 5: 提交**

```bash
git add app/routers/search.py app/observability.py tests/test_search_observability.py
git commit -m "feat(ops): record terminal search metrics"
```

---

### Task 5: Chat SSE 终态指标与真实阶段跨度

**Files:**
- Modify: `app/observability.py`
- Modify: `app/routers/chat.py`
- Modify: `tests/test_chat_usage.py`
- Create: `tests/test_chat_observability.py`

**Step 1: 写失败测试**

至少覆盖：

1. 正常 Chat：完整消费 SSE 后只写一次 `succeeded`；rewrite/retrieve/rerank/answer spans 的 `start_ms` 非递减，Token 只取最终 answer。
2. 早退拒答：`outcome="refused"`，rewrite 可计时，`llm_ms/model/prompt_tokens/completion_tokens/citation_ok` 全为 `None`。
3. pipeline 异常：SSE error 帧保持不变，只写一次 `error`。
4. 消费 hits 后关闭流：写 `cancelled`，不得再写 succeeded。
5. `TimedReranker.rerank()` 原样返回结果并记录耗时。
6. recorder 写库失败不改变 hits、delta、`[DONE]` 帧。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_chat_observability.py tests/test_chat_usage.py -q`

Expected: FAIL，终态指标和 spans 不存在。

**Step 3: 扩展包装器**

`UsageRecordingChatClient` 保留 `.last`，新增 `.calls`；每次调用记录 `{result,start_ms,duration_ms}`。构造器接受 `origin`。`TimedReranker` 代理 `.rerank()`，接口与现有 reranker 完全一致，记录 `.last_ms/.last_span`。

在 Chat：

- 增加 `request: Request` 参数；设置 `request.state.trace_id/kb_id`。
- `origin = time.perf_counter()` 在创建 wrappers 前取得，event stream 复用同一 origin。
- 先包装 reranker，再传给 `build_pipeline`，保留现有 retriever 包装。
- 当 `chunks` 非空时，最后一个 chat call 是 answer，之前的是 rewrite；当 `chunks` 为空时，所有 chat calls 都只能算 rewrite。
- spans 按 `start_ms` 排序，stage 名只允许 `rewrite/retrieve/rerank/answer`。

实现幂等终态函数：

```python
metric_recorded = False


async def finalize_metrics(outcome: str, **overrides) -> None:
    nonlocal metric_recorded
    if metric_recorded:
        return
    metric_recorded = True
    fields = build_metric_fields(outcome=outcome, **overrides)
    await asyncio.shield(
        run_in_threadpool(try_record_rag_metric, engine, **fields)
    )
```

`build_metric_fields` 必须根据 wrappers 的当前快照生成字段，正常答案 `outcome="succeeded"`，拒答为 `refused`，异常为 `error`，取消为 `cancelled`。正常路径在最后一个 `[DONE]` 帧 yield 之后 finalize，使 `total_ms` 包含完整 SSE；pipeline 异常在 error 帧前 finalize；`CancelledError` 在重新抛出前 finalize。

**Step 4: 验证 GREEN 与全量回归**

Run: `py -3.13 -m pytest tests/test_chat_observability.py tests/test_chat_usage.py tests/test_trace.py -q`

Run: `py -3.13 -m pytest -q`

**Step 5: 提交**

```bash
git add app/observability.py app/routers/chat.py tests/test_chat_usage.py tests/test_chat_observability.py
git commit -m "feat(ops): capture terminal chat metrics and stage spans"
```

---

## P1：专业指标与 Trace API

### Task 6: 模型价格配置与成本估算

**Files:**
- Create: `src/ops_pricing.py`
- Create: `tests/test_ops_pricing.py`

**Step 1: 写失败测试**

覆盖：空配置、合法配置、非法 JSON、负价格、NaN、未知模型、部分模型覆盖。

```python
def test_cost_marks_partial_coverage() -> None:
    pricing = parse_pricing(json.dumps({
        "m1": {"prompt_per_1m": 1.0, "completion_per_1m": 2.0}
    }))
    result = estimate_cost([
        {"model": "m1", "prompt_tokens": 1000, "completion_tokens": 500},
        {"model": "unknown", "prompt_tokens": 100, "completion_tokens": 50},
    ], pricing)
    assert result["estimated_cost_usd"] == 0.002
    assert result["pricing_complete"] is False
    assert result["unpriced_tokens"] == 150


def test_no_pricing_returns_null_cost() -> None:
    assert estimate_cost([{"model": "m", "prompt_tokens": 1, "completion_tokens": 1}], {})["estimated_cost_usd"] is None
```

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_pricing.py -q`

**Step 3: 实现**

- `load_pricing_from_env()` 只读 `OPS_MODEL_PRICING_JSON`。
- `parse_pricing(raw)` 返回规范字典；空字符串返回 `{}`。
- JSON 必须是 object；每项只含 `prompt_per_1m/completion_per_1m`，值为有限非负数，否则抛 `ValueError`。
- API 捕获配置错误并返回 `pricing_configured=False/pricing_complete=False/estimated_cost_usd=None`，同时 log，不返回 500。
- 使用 `Decimal` 计算，最终转 float 并 round 6；`priced_tokens/unpriced_tokens` 为整数。
- 未配置与配置了零价格必须可区分：用 `pricing_configured`，不能把零价格当成未配置。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_ops_pricing.py -q`

Run: `py -3.13 -m pytest -q`

```bash
git add src/ops_pricing.py tests/test_ops_pricing.py
git commit -m "feat(ops): add model-aware token cost estimation"
```

---

### Task 7: 时间窗聚合与摘要 API

**Files:**
- Create: `src/ops_observability_report.py`
- Modify: `src/ops_metrics.py`
- Modify: `app/routers/ops.py`
- Create: `tests/test_ops_observability_report.py`
- Modify: `tests/test_ops_summary_api.py`
- Modify: `tests/test_ops_metrics.py`

**Step 1: 写失败测试**

使用 `metric_scope` 插入固定时间数据，向 `summarize_observability(..., now=fixed)` 传固定终点：

- QPM 使用整个窗口分钟数。
- 4xx/5xx 分开；p50/p95/p99 使用原始行。
- Zero-hit 分母含零命中、排除 error/cancelled。
- Refusal 分母只含 Chat 的 succeeded/refused。
- 未执行 LLM/rerank 的行不进入对应分位数样本。
- 汇总价格为部分覆盖时返回部分估算标记。
- 旧 `ai` 字段映射到新口径，响应新增 `observability`，不再调用 `trace_store.list_records`。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_observability_report.py tests/test_ops_summary_api.py tests/test_ops_metrics.py -q`

Expected: FAIL，report 模块不存在，摘要 exact key set 尚未包含 `observability`。

**Step 3: 实现窗口与 SQL 聚合**

```python
WINDOWS = {
    "1h": (timedelta(hours=1), 60),
    "24h": (timedelta(hours=24), 300),
    "7d": (timedelta(days=7), 3600),
    "30d": (timedelta(days=30), 21600),
}


def resolve_window(name: str, *, now: datetime | None = None) -> tuple[datetime, datetime, int]:
    if name not in WINDOWS:
        raise ValueError("invalid window")
    delta, bucket_seconds = WINDOWS[name]
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return end - delta, end, bucket_seconds
```

`summarize_observability(engine, tenant_id, kb_id, window, pricing, now=None)` 使用两条 SQL：

- HTTP：`WHERE tenant_id=:t AND kb_id=:kb AND occurred_at>=:start AND occurred_at<:end`，返回 count、4xx、5xx、`percentile_cont(...) WITHIN GROUP (ORDER BY duration_ms)`。
- RAG：同样按 Tenant/KB/半开时间窗过滤，使用 `FILTER (WHERE ...)` 分别计算各分母、Token 和各阶段分位数。
- 按 model 汇总 Token，交给 `estimate_cost`；不要把未知模型价格当零。
- 所有 rate 在分母为零时返回 `None`；所有分位数返回 `{p50,p95,p99,samples}`。
- HTTP 与 RAG 的样本量分开返回，禁止混用。

修改 `build_ops_summary`，接收 `observability` 与 `latest_traces`，不再接收原始 `traces` 列表。保留 `health/queue/ai/latest_runs/latest_traces/workers` 并追加 `observability`；旧 `ai` 从新结果映射。

本 Task 同时在 report 模块实现基础 `list_rag_metric_rows(engine, tenant_id, kb_id, *, limit=8)`：只按 Tenant/KB 过滤，使用绑定参数限制行数，按 `(occurred_at DESC, id DESC)` 排序并返回摘要所需字典；此时尚不实现筛选和游标。Task 9 再扩展 `window/outcome/operation/mode/min_ms/trace_id/before_occurred_at/before_id` 可选参数，保持这里的默认调用兼容。

`ops_summary` 删除 `trace_store.list_records` 调用，改调 report 和 `list_rag_metric_rows(engine, tenant_id, kb_id, limit=8)`；`window` 默认 `24h`，非法窗口返回 422，不泄露 SQL 错误。

**Step 4: 验证 GREEN 与全量回归**

Run: `py -3.13 -m pytest tests/test_ops_observability_report.py tests/test_ops_summary_api.py tests/test_ops_metrics.py -q`

Run: `py -3.13 -m pytest -q`

**Step 5: 提交**

```bash
git add src/ops_observability_report.py src/ops_metrics.py app/routers/ops.py tests/test_ops_observability_report.py tests/test_ops_summary_api.py tests/test_ops_metrics.py
git commit -m "feat(ops): serve windowed professional metrics"
```

---

### Task 8: 固定桶趋势 API

**Files:**
- Modify: `src/ops_observability_report.py`
- Modify: `app/routers/ops.py`
- Create: `tests/test_ops_timeseries_api.py`

**Step 1: 写失败测试**

- `1h/24h/7d/30d` 分别产生 60/288/168/120 个桶。
- 桶边界 `[start,end)`；边界样本只出现一次。
- 无请求桶的 count 为 0，分位数为 `None`。
- 窗口总 p95 不得由桶 p95 平均。
- Token/cost 与 summary 同口径。
- 跨租户 KB 404；非法 window 422。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_timeseries_api.py -q`

Expected: 404，新端点不存在。

**Step 3: 实现**

桶表达式使用绑定参数，不能插值用户输入：

```sql
to_timestamp(
    floor(extract(epoch from occurred_at) / :bucket_seconds) * :bucket_seconds
) AS bucket
```

分别查询 HTTP 与 RAG 桶数据，再按 `bucket_epoch` 合并到 Python 预建的固定数组。桶数组起点必须是 `floor(start_epoch / bucket_seconds) * bucket_seconds`，之后每次增加一个桶长，共 `bucket_count` 项；SQL 仍按原始 `[start,end)` 过滤，因此首尾桶可以是部分桶。不得从未对齐的 `start` 直接递增，也不得把窗口外的整桶样本计入。响应：

```python
{
    "window": "1h",
    "bucket_seconds": 60,
    "start_at": start.isoformat(),
    "end_at": end.isoformat(),
    "series": [
        {
            "ts": bucket.isoformat(),
            "requests": 0,
            "client_errors": 0,
            "server_errors": 0,
            "e2e_p95_ms": None,
            "retrieval_p95_ms": None,
            "llm_p95_ms": None,
            "total_tokens": 0,
            "estimated_cost_usd": None,
            "pricing_complete": False,
        }
    ],
}
```

新增 `GET /v1/ops/metrics/timeseries`，`kb_id` 必填，`window` 默认 `24h`。趋势只在报告模块中查询数据库，不读 JSONL。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_ops_timeseries_api.py -q`

Run: `py -3.13 -m pytest -q`

```bash
git add src/ops_observability_report.py app/routers/ops.py tests/test_ops_timeseries_api.py
git commit -m "feat(ops): add fixed-bucket observability trends"
```

---

### Task 9: Trace 列表游标与详情 API

**Files:**
- Modify: `src/ops_observability_report.py`
- Modify: `app/routers/ops.py`
- Create: `tests/test_ops_trace_list_api.py`

**Step 1: 写失败测试**

- 新 `GET /v1/ops/trace-list` 返回 `{items,next_cursor}`，稳定按 `(occurred_at DESC,id DESC)`。
- `limit+1` 探测下一页；游标两参数必须成对出现。
- outcome/operation/mode/min_ms/trace_id/window 过滤正确。
- 详情合并 metric、stage_spans 和 JSONL events；原始事件按 KB 再过滤。
- 只有 metric 或只有 JSONL 时，`missing` 明确指出；两者都无则 404。
- 跨租户列表/详情不泄露资源。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_ops_trace_list_api.py -q`

Expected: 404。

**Step 3: 实现**

`list_rag_metric_rows` 的游标条件：

```sql
AND (
    occurred_at < :before_occurred_at
    OR (occurred_at = :before_occurred_at AND id < :before_id)
)
```

只在两参数同时存在时加入；查询 `limit+1`，若查到探测行则先移除，再根据本页实际返回的最后一项生成 `next_cursor`，否则为 `null`。`trace_id` 精确匹配，不使用模糊查询泄露其他记录。

新增：

- `GET /v1/ops/trace-list`：`kb_id` 必填，limit 1–100，默认 20，window 默认 `24h`。
- `GET /v1/ops/traces/{trace_id}/detail`：`kb_id` 必填，返回 `{metric,events,missing}`。
- 保留现有 `GET /v1/ops/traces` 和 `GET /v1/ops/traces/{trace_id}` 行为不变。

`missing` 只能包含 `metric` 或 `events`；没有缺失时为空数组。Detail 的原始事件继续使用 `TraceStore.find`，不得用于 Overview 聚合。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_ops_trace_list_api.py tests/test_ops_jobs_traces_api.py -q`

Run: `py -3.13 -m pytest -q`

```bash
git add src/ops_observability_report.py app/routers/ops.py tests/test_ops_trace_list_api.py
git commit -m "feat(ops): add cursor traces and inspector details"
```

---

## P2：并行构建 WorkBuddy 前端

### Task 10: 新应用壳、设计令牌与 ES module 核心

**Files:**
- Create: `app/static/workbench.html`
- Create: `app/static/css/workbench.css`
- Create: `app/static/js/core.mjs`
- Create: `app/static/js/api.mjs`
- Create: `app/static/js/app.mjs`
- Create: `tests/test_workbench_ui.py`
- Create: `tests/js/core.test.mjs`

**Step 1: 写失败测试**

Python 静态测试请求 `/ui/workbench.html`、`/ui/css/workbench.css`、`/ui/js/app.mjs`，检查七个 nav/view、全局 scope、hash router 和 module script；旧 `/ui/` 测试此时必须保持不变。

```python
NAV = ("overview", "lab", "traces", "ingestion", "evaluation", "access", "audit")


def test_workbench_shell(client) -> None:
    html = client.get("/ui/workbench.html").text
    assert "<title>RAG Operations Console</title>" in html
    assert 'class="wb-sidebar"' in html
    assert 'class="wx-rail"' not in html
    assert 'type="module" src="/ui/js/app.mjs"' in html
    for name in NAV:
        assert f'data-nav="{name}"' in html
        assert f'data-view="{name}"' in html


def test_workbench_core_modules(client) -> None:
    assert "javascript" in client.get("/ui/js/app.mjs").headers["content-type"]
    assert client.get("/ui/css/workbench.css").status_code == 200
```

Node 测试 `escapeHtml`、数字/百分比/空值格式、epoch 快照失效：

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { escapeHtml, formatMetric, ScopeGuard } from "../../app/static/js/core.mjs";

test("escapeHtml escapes all dynamic text", () => {
  assert.equal(escapeHtml(`<img src=x onerror="x">`), "&lt;img src=x onerror=&quot;x&quot;&gt;");
});

test("missing metric is em dash not zero", () => {
  assert.equal(formatMetric(null, "ms"), "—");
});

test("stale scope response is rejected", () => {
  const guard = new ScopeGuard();
  const snapshot = guard.snapshot();
  guard.invalidate();
  assert.equal(guard.isCurrent(snapshot), false);
});
```

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --test tests/js/*.test.mjs`

Expected: 404 与模块不存在。

**Step 3: 实现应用壳**

`workbench.html` 包含：

- `.wb-sidebar`、品牌、七个 `data-nav`。
- `.wb-topbar`：`scope-tenant/scope-kb/scope-user/scope-window/scope-refresh/app-status`。
- 七个 `data-view`，默认仅 Overview 不 hidden。
- `<script type="module" src="/ui/js/app.mjs"></script>`。
- 无外部字体、图片、CDN 或框架依赖。

`core.mjs` 至少完整实现：

```javascript
export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

export function formatMetric(value, unit = "", digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export class ScopeGuard {
  constructor() { this.epoch = 0; this.controller = new AbortController(); }
  snapshot() { return { epoch: this.epoch, signal: this.controller.signal }; }
  invalidate() { this.epoch += 1; this.controller.abort(); this.controller = new AbortController(); }
  isCurrent(snapshot) { return snapshot.epoch === this.epoch; }
}
```

`api.mjs` 提供 `scopeHeaders(scope,{json=false}={})`、`apiJson(path,{scope,signal,...options})` 和 `ApiError`；JSON body 必须 `JSON.stringify`，FormData 不得手动设置 Content-Type。

`app.mjs` 实现 `showView`、hash 解析/恢复、scope 变更时 `ScopeGuard.invalidate()`、七模块导航和刷新事件。各视图收到 scope 变更后必须清空上一 scope 的业务数据与选中状态，不得仅 abort 请求而继续展示旧内容。CSS 使用白色 surface、浅灰背景、细边框、蓝紫 accent，包含 `tabindex`/focus-visible、数字等宽和响应式基础。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py tests/test_api_ui.py -q`

Run: `node --check app/static/js/app.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/workbench.html app/static/css/workbench.css app/static/js/core.mjs app/static/js/api.mjs app/static/js/app.mjs tests/test_workbench_ui.py tests/js/core.test.mjs
git commit -m "feat(ui): add WorkBuddy application shell"
```

---

### Task 11: Overview 专业卡片与 SVG 趋势

**Files:**
- Create: `app/static/js/views/overview.mjs`
- Modify: `app/static/workbench.html`
- Modify: `app/static/css/workbench.css`
- Modify: `app/static/js/app.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

检查 Overview 必须包含：Requests、Avg QPM、4xx/5xx、E2E/Retrieve/LLM p95、Zero-hit、Refusal、Citation、Token、Cost、Queue、Workers、窗口、样本量和趋势 SVG 容器；必须引用 `overview.mjs`。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

**Step 3: 实现**

- `loadOverview(scopeSnapshot)` 并行但独立调用 summary 与 timeseries；任一失败不影响另一面板。
- 每次响应先检查 `ScopeGuard.isCurrent`，过期响应直接丢弃。
- 卡片同时展示值、单位、窗口、样本量；空值为 `—`，低样本显示提示。
- 成本区分“未配置”“部分估算”“完整估算”。
- `renderTrend(series)` 使用内联 SVG；空序列显示空状态，不渲染伪造折线。
- 每 15 秒刷新一次，仅当前模块为 Overview 且 `document.visibilityState === "visible"` 时请求；离开模块立即清理 timer。
- 仅同一 scope 可保留上次成功数据；局部刷新失败时显示 `stale-at` 和最后成功时间，scope 变更后不得沿用。

SVG 生成只允许数值进入坐标，文本走 `escapeHtml`；不得把原始 JSON 注入属性。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/views/overview.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/js/views/overview.mjs app/static/workbench.html app/static/css/workbench.css app/static/js/app.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): add professional overview and trends"
```

---

### Task 12: Retrieval Lab 全量迁移

**Files:**
- Create: `app/static/js/views/lab.mjs`
- Modify: `app/static/workbench.html`
- Modify: `app/static/css/workbench.css`
- Modify: `app/static/js/app.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

检查内部 Tab `search/chat/ingest`，保留旧功能关键 ID：`upload-form/upload-input/upload-progress/search-form/search-results/chat-form/answer/citation-list/stream-meta`，并新增 `chat-trace-link/search-trace-link`。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

**Step 3: 实现**

- 上传：`POST /v1/kbs/{kb_id}/documents`，FormData，轮询 `GET /v1/jobs/{job_id}` 直到终态；离开作用域时 abort。
- Search：请求体只含 `query/top_k/mode/audience`，展示 score、chunk_id、doc_id、metadata；从 `X-Trace-Id` 设置 Trace 跳转。
- Chat：`POST /v1/chat/stream`，body 含 `kb_id/question/history/top_k/audience`，不得含 tenant_id。
- SSE 按 `\n\n` 分帧，处理 `hits`、普通 delta、`event:error`、`[DONE]`；跨 chunk 半帧必须保留 buffer。
- hits 帧展示 usage/timings/trace_id；答案中的 `[chunk_id]` 与引用按钮可展开原文，检索缓存优先，缓存 miss 再调 Search。
- 新 Trace 链接使用 `#/traces/{id}`；无 trace id 时隐藏。
- 所有动态正文、metadata、错误信息转义；请求失败恢复按钮，scope 变化取消流并清空旧答案。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/views/lab.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/js/views/lab.mjs app/static/workbench.html app/static/css/workbench.css app/static/js/app.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): migrate retrieval lab and SSE chat"
```

---

### Task 13: Traces 三栏诊断与 Trace Inspector

**Files:**
- Create: `app/static/js/views/traces.mjs`
- Modify: `app/static/workbench.html`
- Modify: `app/static/css/workbench.css`
- Modify: `app/static/js/app.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

检查筛选字段、列表、详情、Inspector 三 Tab、游标加载、复制按钮、响应式抽屉和空状态 marker。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

**Step 3: 实现**

- 列表调用 `/v1/ops/trace-list`，支持 window/outcome/operation/mode/min_ms/trace_id/limit 和 cursor。
- 选中项调用 `/v1/ops/traces/{id}/detail?kb_id=...`；hash 保存选中 id，刷新后恢复。
- 列表列：时间、trace、operation、outcome、hits、E2E、Token。
- 瀑布图：`left = start_ms / total_ms * 100`、`width = duration_ms / total_ms * 100`；total 为 0 时只显示数值，不计算百分比。
- `other/uncounted = max(total_ms - max(span.end), 0)`；缺失 metric/events 时展示 `missing` 说明。
- Inspector Summary/Timing/Raw 可键盘切换；Raw 用 `textContent` 渲染格式化 JSON，复制必须经用户点击。
- 刷新列表不得改变当前 Trace、滚动位置或筛选；加载更多保留已有项目。
- `>=1440px` 三栏，`900–1439px` Inspector 抽屉，`<900px` 列表/详情内部 Tab；抽屉有 focus trap、Escape 关闭和焦点归还。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/views/traces.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/js/views/traces.mjs app/static/workbench.html app/static/css/workbench.css app/static/js/app.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): add trace inspector and diagnostics"
```

---

### Task 14: Ingestion 与 Evaluation 工作区

**Files:**
- Create: `app/static/js/views/ingestion.mjs`
- Create: `app/static/js/views/evaluation.mjs`
- Create: `app/static/js/permissions.mjs`
- Modify: `app/static/workbench.html`
- Modify: `app/static/css/workbench.css`
- Modify: `app/static/js/app.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

检查 jobs 表、job events、retry，runs 指标/历史/详情/delta、eval/loadtest 触发及三种 drill 按钮 marker。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

**Step 3: 实现**

先创建 `permissions.mjs`，统一实现 `canActions/canRetry/canCompare` 及对应原因，读取 `/v1/identity` 与 `/v1/ops/permissions` 的当前快照；Ingestion/Evaluation 在本 Task 即接入，Task 15 只复用并扩展 Access 展示，不得再建一套权限判断。

Ingestion：

- `/v1/ops/jobs` 与 `/v1/ops/jobs/{id}/events` 独立加载、独立错误状态。
- 显示 wait/duration/progress/status/error；重试仅 failed 且管理员动作开启时可用。
- 409 显示“已有运行中的任务”，404 按不可见处理；不得伪造成功。

Evaluation：

- `/v1/ops/runs`、`/v1/ops/runs/{id}` 复用现有响应。
- eval_mock/loadtest 与 drill_parser/drill_llm/drill_queue 分开显式绑定，禁止循环索引错配。
- 触发使用现有 `/v1/ops/kbs/{kb}/runs`，成功 202 后定位详情并轮询，409 恢复按钮。
- 展示 exit_code、artifact_path、output_tail、error、summary 指标；与上一个同 kind 已完成运行计算 delta，缺失为 `—`。
- 指标键与 `metrics.recall_at_k/mrr/hit_rate/refusal_accuracy/answer_coverage/citation_accuracy` 一致。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/views/ingestion.mjs; node --check app/static/js/views/evaluation.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/js/views/ingestion.mjs app/static/js/views/evaluation.mjs app/static/js/permissions.mjs app/static/workbench.html app/static/css/workbench.css app/static/js/app.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): migrate ingestion and evaluation workspaces"
```

---

### Task 15: Access、Audit 与全局权限状态

**Files:**
- Create: `app/static/js/views/access.mjs`
- Create: `app/static/js/views/audit.mjs`
- Modify: `app/static/js/permissions.mjs`
- Modify: `app/static/workbench.html`
- Modify: `app/static/css/workbench.css`
- Modify: `app/static/js/app.mjs`
- Modify: `tests/test_workbench_ui.py`

**Step 1: 写失败测试**

检查 identity、三层隔离、权限原因、compare 表单/结果、audit timeline，以及统一动作禁用说明 marker。

**Step 2: 验证 RED**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

**Step 3: 实现**

- `/v1/identity` 与 `/v1/ops/permissions` 独立加载；identity 失败不能清空 KB 选择。
- Access 展示 tenant/user/role/tags、KB scoped counts、三层隔离与 reason。
- Compare 使用现有 `/v1/ops/kbs/{kb}/identity-compare`，query 1–500、user_ids 1–3、top_k 1–10；结果按用户分列展示 hits 与 chunk ids。
- Audit 使用 `/v1/ops/audit?limit=50`，按时间升序展示 actor/action/target/result/reason。
- 复用 Task 14 的 `permissions.mjs`，按需扩展 Access 展示所需原因；`canActions/canRetry/canCompare` 仍由同一模块供 Ingestion/Evaluation/Access 使用。
- 非管理员和开关关闭是两种不同原因；前端 disabled 不代替后端校验。
- scope 改变清空旧 identity、compare、audit 和权限缓存，旧响应不得覆盖。

**Step 4: 验证 GREEN、全量回归并提交**

Run: `py -3.13 -m pytest tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/views/access.mjs; node --check app/static/js/views/audit.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

```bash
git add app/static/js/views/access.mjs app/static/js/views/audit.mjs app/static/js/permissions.mjs app/static/workbench.html app/static/css/workbench.css app/static/js/app.mjs tests/test_workbench_ui.py
git commit -m "feat(ui): migrate access audit and permission states"
```

---

## P3：入口切换与最终验收

### Task 16: 切换 `/ui`、响应式/键盘验收与文档

**Files:**
- Modify: `app/static/index.html`
- Delete: `app/static/workbench.html`
- Modify: `tests/test_api_ui.py`
- Modify: `tests/test_workbench_ui.py`
- Modify: `docs/ops-cockpit-checklist.md`

**Step 1: 先改测试，验证 RED**

将 `tests/test_workbench_ui.py` 的入口从 `/ui/workbench.html` 改为 `/ui/`；重写 `tests/test_api_ui.py`，移除 wx 三栏断言，改为：

- 七个模块唯一且视图默认只显示 Overview。
- 无 `wx-rail/wx-middle/wx-content`。
- 新 ES modules 与全部专业面板存在。
- 旧关键能力 ID 仍存在。
- 静态资源可在任意 cwd 访问。

Expected: FAIL，旧 index 仍是 wx 页面。

**Step 2: 切换入口**

用最终 `app/static/workbench.html` 内容替换 `app/static/index.html`，然后删除临时 `workbench.html`。不得保留两套入口或复制旧脚本到新页面。

**Step 3: 验证全量自动化**

Run: `py -3.13 -m pytest tests/test_api_ui.py tests/test_workbench_ui.py -q`

Run: `node --check app/static/js/app.mjs; node --test tests/js/*.test.mjs`

Run: `py -3.13 -m pytest -q`

Expected: 全部 PASS；原 469 项回归及本计划新增测试均通过。

**Step 4: 真实浏览器验收**

启动当前源码 API（动作测试时使用 `OPS_ENABLE_ACTIONS=1`），必要时启动 worker 与 `scripts/run_ops.py`。至少验证：

- 1440、1280、900、390px：无横向溢出，三栏/抽屉/单栏按设计切换。
- 仅键盘：导航、内部 Tab、scope、检索、SSE、Trace 选择、抽屉、复制、触发和重试。
- Overview 四档窗口、空数据、低样本、价格未配置/部分配置、局部失败和 stale。
- Search/Chat/拒答/异常/取消产生正确指标与 Trace；SSE 内容与原协议一致。
- Trace 游标、筛选、深链接、Raw 复制、缺失 metric/events。
- 上传、入库、重试、评测、压测、三项演练、身份对比、审计。
- 非管理员和跨租户访问；切换 scope 时旧响应被丢弃。
- 临时让指标写入失败，Search/Chat/SSE 仍成功。

把实际证据、未覆盖项和结果追加到 `docs/ops-cockpit-checklist.md`，不得只写“通过”而没有操作与观察结果。

**Step 5: 最终提交**

```bash
git add app/static/index.html app/static/workbench.html tests/test_api_ui.py tests/test_workbench_ui.py docs/ops-cockpit-checklist.md
git commit -m "feat(ui): switch to WorkBuddy observability console"
```

---

## 最终整体审查

全部 Task 完成后，派最终 code reviewer 检查：

1. Spec 逐项覆盖，无计划外功能或缺失模块。
2. 指标分母、时间边界、分位数、Token、成本与游标稳定性的正确性。
3. HTTP/SSE/取消/异常只写一次，采集失败不影响业务。
4. Tenant/KB 隔离、权限、审计、XSS 与原始 JSON 安全。
5. 前端旧能力完整、无隐藏轮询、无过期响应覆盖、无无效依赖。
6. 全量 pytest、Node 测试和真实浏览器证据完整。
7. 最终 diff 不含他人 dirty/untracked 文件，不覆盖 eval baseline，不修改 `src/pipeline.py`。

审查问题必须修复并复审后，再按 `superpowers-finishing-a-development-branch` 收尾。
