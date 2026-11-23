# API

The authoritative contract is the OpenAPI document served by the application at `/docs` (interactive) and `/openapi.json` (machine-readable). This page is a map of the surface, request conventions and error semantics; when the two disagree, `/docs` and the router code win.

Base URL locally: `http://127.0.0.1:8000`. Behind the demo gateway: `http://<demo-url>:18000`.

## Conventions

- All `/v1` endpoints return JSON unless stated otherwise. Chat uses `text/event-stream`.
- **Tenant** is taken from the `X-Tenant-Id` header (default `demo`). It is never read from the body or query string. This header is a Phase-3 placeholder for real authentication: today any caller can name a tenant, so production exposure requires TLS and token-derived identity.
- **User** is taken from `X-User-Id` (default `demo-user`). The role and ACL tags are read from the `users` table for that tenant; a header cannot assert a role.
- Request models use `extra="forbid"`: unknown fields, including `tenant_id` and `kb_id`, produce `422`.
- Identifiers (`tenant_id`, `kb_id`) must match `[A-Za-z0-9_-]+`.
- Responses from search and chat carry `X-Trace-Id`; traces are retrievable at `GET /v1/traces/{trace_id}`.

## Endpoints

### Ingestion and storage

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/kbs/{kb_id}/documents` | Multipart upload with field `file`. Supported suffixes: `.pdf`, `.md`, `.txt`, `.docx`, `.pptx`, `.xlsx`. Deduplicates by SHA-256 prefix and returns `202` with `job_id`, `doc_id`, `duplicated`. |
| `GET` | `/v1/storage` | Document list for the tenant. Query: `kb_id`, `status` (`pending`/`ready`/`failed`), `limit` (1–500, default 200). Returns `backend`, `bucket`, `stats` and `documents`. |
| `GET` | `/v1/jobs/{job_id}` | One ingestion job: `status`, `attempts`, `progress`, `error`, `source`, timestamps. |
| `GET` | `/v1/jobs` | Most recent 20 jobs for `kb_id`, newest first. |

### Retrieval and generation

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/kbs/{kb_id}/search` | Body: `query` (1–2000 chars), `top_k` (1–50, default 5), `mode` (`hybrid` default, `vector`, `bm25`), `audience` (`public` default, `internal`). Returns hits with `chunk_id`, `doc_id`, `score`, `text`, `metadata`. |
| `POST` | `/v1/chat/stream` | Body: `kb_id`, `question` (1–2000), `history` (list of message dicts), `top_k` (1–20, default 5), `audience`. Server-Sent Events stream. |

SSE frames from `/v1/chat/stream`:

```text
event: hits
data: {"chunks":["kb-after-sale#1"],"rewritten":"...","citation_ok":true,"trace_id":"..."}

data: {"delta": "退款审核通过后"}
data: {"delta": "按原支付路径退回"}
data: [DONE]
```

On failure the stream emits `event: error` with a `message` field and ends without `[DONE]`. Refusals are delivered as a normal answer with an empty `chunks` list or a refusal text, not as an HTTP error.

### Observability

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/traces/{trace_id}` | Recorded trace stages for a search or chat request. |
| `GET` | `/v1/stats` | Aggregated job and Q&A statistics for `kb_id`; optional `top_n` (1–50, default 10). |
| `GET` | `/v1/identity` | Current identity card: user, role, tags, visible knowledge bases. |
| `GET` | `/v1/ops/summary` | Cockpit summary for `kb_id`: component health, queue counts, metrics, recent traces and runs. |
| `GET` | `/v1/ops/metrics/timeseries` | Metric time series for `kb_id` over `window` (default `24h`). |
| `GET` | `/v1/ops/jobs` | Job list with per-status counts and derived wait/duration seconds. |
| `GET` | `/v1/ops/jobs/{job_id}/events` | Stage event timeline for one job. |
| `GET` | `/v1/ops/trace-list` | Filterable trace list (outcome, operation, mode, min duration, cursor pagination). |
| `GET` | `/v1/ops/traces` | Recent traces for `kb_id`. |
| `GET` | `/v1/ops/traces/{trace_id}/detail` | Trace metric plus event spans; reports which parts are missing. |
| `GET` | `/v1/ops/permissions` | Explanation of a visibility decision: ACL, RLS or document state. |
| `POST` | `/v1/ops/kbs/{kb_id}/identity-compare` | Compare access for guest/member/admin identities. |
| `GET` | `/v1/ops/storage/embedding-drift` | Per-knowledge-base embedding version distribution, `drift_chunks`, `configured_version`. Optional `kb_id`. |

### Operations actions

These require `OPS_ENABLE_ACTIONS=1` in the API environment and an admin user; otherwise `403`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/ops/kbs/{kb_id}/runs` | Queue an allow-listed run: `eval_mock`, `loadtest`, `drill_parser`, `drill_llm`, `drill_queue`. Returns `202` with `run_id`. |
| `GET` | `/v1/ops/runs` | List runs for `kb_id`, optionally filtered by `kind`. |
| `GET` | `/v1/ops/runs/{run_id}` | One run record. |
| `POST` | `/v1/ops/jobs/{job_id}/retry` | Requeue a `failed` job. Returns `202`; `409` when the job is not failed. |
| `GET` | `/v1/ops/audit` | Allowed/denied action audit entries for the tenant. |

### Health and console

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Service name and `APP_ENV`. |
| `GET` | `/live` | Liveness; always `200` while the process is up. |
| `GET` | `/ready` | Readiness; `200` with `{"ok":true,"db":"up"}` or `503` with `{"ok":false,"db":"down"}`. Cached for 5 seconds. |
| `GET` | `/ui/` | Static operations console (no build step). |
| `GET` | `/docs`, `/openapi.json` | Interactive and machine-readable API contract. |

Behind the demo gateway, `GET /health` is an anonymous alias for `/live`; every other path requires Basic Auth.

## Error semantics

| Status | Meaning in this service |
|---|---|
| `400` | Malformed identifier. `invalid tenant_id` or `invalid kb_id` when an ID fails the character whitelist, or `invalid kb_id` on ops filters. |
| `403` | Authorisation failure. `admin required` when the user is not an admin, or `ops actions disabled` when `OPS_ENABLE_ACTIONS` is not `1`. |
| `404` | Resource absent or outside the caller's tenant: `knowledge base not found`, `job not found`, `run not found`, `trace not found`. Existence is deliberately not distinguished from ownership. |
| `409` | Conflicting state: a run of the same kind is already active for the knowledge base, or a retry was requested for a job that is not `failed`. |
| `413` | `file too large`: the upload exceeds `MAX_UPLOAD_BYTES` (default 52428800). |
| `422` | Request validation: schema violations, unknown fields, `unsupported file type`, `empty file`, invalid `window`, unpaired pagination cursor, non-finite numbers. |
| `429` | Rate limited by the nginx gateway (`limit_req_status 429`, 10 r/s with burst 20 per client IP). Not generated by the application itself. |
| `500` | Internal defect or failed side effect: `failed to persist upload` when the object write fails, or `internal scope error` when a chunks query was built without tenant scope. Unhandled errors return `Internal Server Error` with an `X-Trace-Id` header. |

`503` is returned by `/ready` when the database probe fails and by chat dependencies when the chat client cannot be constructed (`LLM 未配置`).
