# Roadmap

This roadmap lists acknowledged gaps between the current implementation and a production-hardened service. It contains no delivery dates; each item is defined by an acceptance criterion that can be verified against the repository or a running deployment. Items are grouped by priority, not by sequence.

## P1 — required for production exposure

### JWT/OIDC authentication with RBAC

**Current gap:** tenant and user identity come from `X-Tenant-Id` and `X-User-Id` headers (`app/deps.py`), which any caller can set. Roles are read from `users.role`, but there is no token verification.

**Acceptance criteria:**

- The API rejects requests without a valid signed token and derives tenant and user from token claims; the header placeholders are removed or ignored.
- `users.role` is sourced from a directory/IdP group mapping and cannot be self-asserted.
- Tests cover: missing token, expired token, wrong tenant claim, non-admin calling an ops action, and an admin calling the same action successfully.

### TLS termination

**Current gap:** the demo gateway serves plain HTTP on `18000`; Basic Auth credentials and request bodies are unencrypted.

**Acceptance criteria:**

- The public entry point serves HTTPS with a certificate that validates against a public CA (or an internal CA with documented trust distribution).
- HTTP requests are redirected to HTTPS; HSTS is configured.
- SSE chat still streams end to end through the TLS terminator without buffering.

### Prometheus metrics and alerting

**Current gap:** metrics are exposed through application endpoints and the console (`/v1/ops/metrics/timeseries`, `/v1/ops/summary`), not in a scrape format, and there are no alert rules.

**Acceptance criteria:**

- A `/metrics` endpoint or sidecar exposes request count, latency histograms, error rate, queue depth, job failure count, refusal rate and LLM token usage in Prometheus format.
- Multi-process API deployment is handled (no duplicated or lost counters).
- Alert rules exist for: `/ready` failing, queue age above a threshold, failed-job rate above a threshold, and embedding drift greater than zero.
- A documented runbook entry maps each alert to an operator action.

### Larger and more diverse evaluation set

**Current gap:** 50 cases in one Chinese consumer-support domain with a 26-chunk corpus; Recall@5 is saturated at `1.0000`.

**Acceptance criteria:**

- At least 300 labelled cases spanning at least two domains and two languages, with a corpus large enough that top-5 retrieval is not saturated (documented baseline below 1.0 Recall@5).
- Labelling follows the existing rules in `eval/README.md`, including multi-label and unanswerable cases.
- A new baseline file is recorded, refusal threshold is re-calibrated, and `scripts/ci_gate.py` runs against the expanded baseline in CI.
- The evaluation report records model names, date and corpus size alongside the metrics.

### Multi-worker ingestion with distributed BM25 rebuild

**Current gap:** the worker is designed as a single replica. Concurrent BM25 snapshot rebuilds for the same knowledge base can interleave, so an older snapshot may be written after a newer one; the Compose file documents this limitation.

**Acceptance criteria:**

- Two or more worker replicas can run concurrently without duplicating work (`SKIP LOCKED` claim remains correct under contention).
- BM25 snapshot publication is serialised per knowledge base (for example via advisory lock or versioned snapshot pointer), and the newest completed ingest always determines the served snapshot.
- A concurrency test runs N workers against a shared database and asserts every document is ingested exactly once and the served snapshot matches the final state.

## P2 — capability expansion

### Additional connectors

**Current gap:** connectors exist for local directories and Feishu (`src/connectors/`). Other enterprise sources require custom work.

**Acceptance criteria:**

- At least one additional connector (for example an object-storage or HTTP-crawl connector) implements the existing connector interface, including content hashing, deduplication, suffix validation and enqueue through `receive_source_document`.
- Connector sync is idempotent: repeated runs create no duplicate documents and no duplicate active jobs.
- Connector failures are isolated per source and recorded as job events rather than crashing the worker loop.

### Incremental indexing

**Current gap:** re-ingesting a document deletes and rewrites all of its chunks; embedding cost scales with document size, not with the changed portion.

**Acceptance criteria:**

- Unchanged chunks of an updated document retain their `chunk_id` and skip re-embedding, verified by comparing stored embeddings and `embed_model_version` before and after.
- Deleted sections remove their chunks; added sections embed only the new text.
- The evaluation baseline does not regress beyond the `scripts/ci_gate.py` threshold after the change, and a regression test asserts the number of embedding calls for a small edit.

### Cost dashboard

**Current gap:** token usage and model names are recorded per chat request, and pricing can be loaded from environment configuration (`src/ops_pricing.py`), but there is no consolidated cost view.

**Acceptance criteria:**

- The console and an API endpoint report cost per tenant and knowledge base over a selectable window, derived from recorded token usage and configured prices.
- Unpriced models are shown explicitly as unpriced instead of being silently costed at zero.
- A test covers aggregation across multiple models and verifies that missing pricing does not corrupt totals.
