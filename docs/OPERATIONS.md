# Operations

This guide covers day-two operation of a running deployment: probes, job diagnosis, recovery procedures, drift remediation, log access and load testing. Degradation drills (parser loss, embedding key removal, database loss) are documented separately in [`runbook.md`](runbook.md).

## Probes

| Endpoint | Meaning | Behaviour |
|---|---|---|
| `GET /live` | Process liveness | Always `200` while the process runs. Container health checks use this endpoint so a database blip does not restart a healthy API. |
| `GET /ready` | Readiness | Opens a short-lived connection, runs `SELECT 1`, returns `200 {"ok":true,"db":"up"}` or `503 {"ok":false,"db":"down"}`. The result is cached for **5 seconds** and the probe has a 2-second connect timeout. |
| `GET /health` (gateway) | Gateway health | Anonymous route on the nginx demo gateway, proxied to `/live`. It intentionally does not test the database. |

Because `/ready` is cached, a single observation immediately after a database restart can still be stale. Wait at least one cache window, or poll until the response changes, before drawing conclusions.

## Job diagnosis

Every ingestion job exposes its status, attempts, progress and last error.

```bash
# Queue and per-status counts for one knowledge base
curl -s -H 'X-Tenant-Id: demo' \
  'http://127.0.0.1:8000/v1/ops/jobs?kb_id=kb_demo'

# Filter to failures
curl -s -H 'X-Tenant-Id: demo' \
  'http://127.0.0.1:8000/v1/ops/jobs?kb_id=kb_demo&status=failed'

# Stage timeline for one job
curl -s -H 'X-Tenant-Id: demo' \
  'http://127.0.0.1:8000/v1/ops/jobs/42/events'
```

`GET /v1/ops/jobs` returns `counts` for `queued`, `running`, `succeeded` and `failed`, plus derived `wait_seconds` and `duration_seconds` per job. `GET /v1/ops/jobs/{job_id}/events` returns the recorded stages (`queued`, `claimed`, `parsing`, `chunking`, `embedding`, `publishing`, `succeeded`, `failed`) with progress values and messages. The same data is rendered in the console under the operations view.

`GET /v1/jobs/{job_id}` returns the same job without the event timeline and is the endpoint to use for client-side polling.

### Manual retry

Retry is allowed only for `failed` jobs and requires two independent conditions:

1. `OPS_ENABLE_ACTIONS=1` in the API process environment (the default `0` exposes a read-only cockpit);
2. the caller's `users.role` is `admin` in the current tenant. Role is read from the database, never from a request header.

```bash
curl -s -X POST -H 'X-Tenant-Id: demo' -H 'X-User-Id: admin-user' \
  'http://127.0.0.1:8000/v1/ops/jobs/42/retry'
```

A successful retry returns `202` with `{"job_id":42,"status":"queued"}`, resets attempts, progress and error, returns the document to `pending`, and appends a `queued` event with `manual retry`. Retrying a job that is not failed returns `409`; an unknown job returns `404`. Both allowed and denied attempts are written to the operations audit log.

## Stale jobs and leases

The worker calls `reclaim_stale_running()` at the start of every loop. A job in `running` whose `updated_at` is older than `JOB_LEASE_SECONDS` (default `900`) is returned to `queued` while `attempts < 3`, or set to terminal `failed` with `error = 'lease expired after 900 seconds'` once attempts are exhausted. Configure a different lease with `JOB_LEASE_SECONDS`; invalid or non-positive values fall back to `900`.

If the worker has been stopped and the queue must be inspected or recovered before it restarts, the equivalent SQL is:

```sql
UPDATE ingest_jobs
SET status = CASE WHEN attempts < 3 THEN 'queued' ELSE 'failed' END,
    progress = CASE WHEN attempts < 3 THEN 0 ELSE progress END,
    error = CASE WHEN attempts < 3 THEN ''
                 ELSE 'lease expired after 900 seconds' END,
    started_at = CASE WHEN attempts < 3 THEN NULL ELSE started_at END,
    finished_at = CASE WHEN attempts < 3 THEN NULL ELSE now() END,
    updated_at = now()
WHERE status = 'running'
  AND updated_at < now() - make_interval(secs => 900)
RETURNING id, tenant_id, doc_id, status;
```

Run it against the `kb` database. The worker performs the same statement automatically, so this is a recovery aid, not a routine step. After recovery, confirm the queue drains and that failed jobs corresponding to terminal rows also have `documents.status = 'failed'`; the worker updates document status only when the job reaches a terminal state.

## Embedding drift

`embed_model_version` is stored per chunk. When `EMBED_MODEL` changes (or a mock vector was written during a keyless run), stored vectors no longer represent the configured model. Retrieval quality degrades silently because vectors from different models are not comparable.

Check drift:

```bash
curl -s -H 'X-Tenant-Id: demo' \
  'http://127.0.0.1:8000/v1/ops/storage/embedding-drift'
```

The response reports `configured_version`, `total_chunks`, `drift_chunks` and a per-knowledge-base breakdown with the count of chunks per stored version. The API also logs a warning at startup when drift exists; the check is best-effort and never blocks startup.

Remediate with the idempotent re-embedding script. Preview first:

```bash
py -3.13 scripts/reembed_chunks.py --tenant demo --dry-run
```

Then run it for real:

```bash
py -3.13 scripts/reembed_chunks.py --tenant demo
```

Scope to one knowledge base with `--kb`, or select by source version with `--only mock-hash|bulk-random|all`. The script batches updates and can be resumed. Re-run the drift endpoint afterwards; `drift_chunks` should reach zero. When the embedding provider changes, re-calibrate the refusal threshold with `scripts/calibrate_refusal.py` before relying on the gate.

## Common failures

| Symptom | Likely cause | Action |
|---|---|---|
| Uploads succeed but search results are semantically meaningless; scores look random | `EMBED_API_KEY` missing or `EMBED_MODE=mock`, so hash vectors were written | Configure a real embedding key, restart api and worker, then re-embed with `scripts/reembed_chunks.py` and re-run the drift check. |
| Job fails with `上传文件不存在` / `NonRetryableIngestError` | The object key in `documents.source_url` is absent from the configured backend (volume not shared, bucket wiped, wrong `STORAGE_BACKEND`) | Restore the object, or re-upload the file. The job will not self-heal because the missing object never appears. |
| PDF job fails with a parser connection error, or scanned PDFs fail locally | `PARSER_BASE_URL` unset or the parser container is down | Start the parser (`--profile parser`) or accept local `pypdf` degradation for text PDFs. Local fallback never performs OCR, so scanned files fail with a message indicating OCR is required. |
| API returns `500 internal scope error` and logs `缺少租户/知识库过滤条件` | A query on `chunks` was built without tenant scope | Code defect: locate the query and route it through `tenant_scope` / a scoped store. This is intentionally a 500, not an empty result. |
| Every chunk query returns 0 rows although data exists | Application connected with a role that bypasses or fails RLS, or `app.tenant_id` was not set for the transaction | Connect as `kb_app` (non-superuser) and use `src/db/session.py` `tenant_scope`. Check `users.tags` and ACL filtering before assuming data loss. |
| `/ready` returns `503` | Database unreachable | Check the `db` container and credentials; remember the 5-second cache. |
| Chat returns `503` with `LLM 未配置` | `LLM_API_KEY` missing in the API process | Add the key and recreate the API container; `restart` alone does not apply a changed container environment. |
| Console loads but shows raw i18n keys | Browser cached an older static module | The API sends `Cache-Control: no-cache` for `/ui`; force-refresh the browser. |

For queue and trace-level diagnosis, the cockpit also exposes `GET /v1/ops/summary`, `GET /v1/ops/trace-list`, `GET /v1/ops/traces` and `GET /v1/ops/traces/{trace_id}/detail`.

## Logs

The demo overlay configures the `json-file` driver with `max-size: 10m` and `max-file: 3` for every service, so each container retains at most 30 MB of logs.

```bash
# Last 100 lines from each service
docker compose -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml \
  logs --tail 100 api worker

# Follow one service
docker compose -f deploy/docker-compose.yml logs -f worker
```

The worker prints one line per processed job (`worker 入库成功` / `worker 入库失败` with `job_id`, `doc_id`, error and chunk count). Pipeline warnings (`rerank 降级`, invalid citations, blocked context) are printed by the API process. Stage-level detail is better read from the job events endpoint than from log scraping.

## Load testing

`scripts/loadtest.py` measures `POST /v1/kbs/{kb_id}/search` and `POST /v1/chat/stream` with bounded asyncio concurrency. `--requests` is per endpoint, so the default run sends 400 requests in total.

```bash
py -3.13 scripts/loadtest.py --concurrency 20 --requests 200 \
  --base-url http://127.0.0.1:8000 --kb kb_demo --tenant demo
```

The report is written to `eval/loadtest_report.json`; override with `--out`. Latency covers the full response or SSE stream, and failed requests are counted in the percentiles rather than discarded. `error_rate` includes non-2xx responses, connection errors, SSE `error` events, missing/invalid `hits` events and a missing `[DONE]`. Percentiles are `null` for empty samples instead of being reported as zero.

Run load tests against a non-production stack; the API and worker share one database, and saturated ingestion will delay interactive queries.

## Routine checklist

1. `/ready` returns `200` and the gateway `/health` returns `200`.
2. `GET /v1/ops/jobs` shows no `failed` jobs older than the current on-call window.
3. `GET /v1/ops/storage/embedding-drift` reports `drift_chunks: 0` after any model change.
4. Container logs show no repeating parser connection errors or citation-validation warnings.
5. A backup exists for the current database and object volumes (see [`DEPLOYMENT.md`](DEPLOYMENT.md)).
