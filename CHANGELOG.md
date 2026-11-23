# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Environment-driven CORS middleware (`CORS_ALLOW_ORIGINS`, disabled by default).
- Configurable upload limit (`MAX_UPLOAD_BYTES`, default 50 MB) enforced before any
  database write or disk write.
- Read-only embedding drift report at `GET /v1/ops/storage/embedding-drift`, plus a
  startup warning when stored vectors do not match `EMBED_MODEL`.
- Idempotent `scripts/reembed_chunks.py` for batch re-embedding with `--dry-run`.
- Worker lease recovery: jobs stuck in `running` longer than `JOB_LEASE_SECONDS`
  (default 900s) are requeued, and fail terminally after `MAX_ATTEMPTS`.
- nginx demo gateway (Basic Auth, per-IP rate limit, 50 MB body limit) and a Compose
  overlay that stops publishing database and object-storage ports.
- `scripts/deploy_demo.sh` for repeatable demo deployment.
- GitHub Actions CI (Python, front-end unit tests, evaluation gate), issue and PR
  templates, Dependabot, ruff configuration, Apache-2.0 license.
- English README with a Chinese mirror and a rewritten `docs/` set.

### Changed

- Answers are gated by semantic confidence (`REFUSAL_MIN_SIMILARITY`, default 0.56)
  instead of the rank-based fusion score, which carried no relevance signal.
- Evaluation snapshots now record generation metrics when run with `--generate`.

## [0.1.0] - 2026-11-23

### Added

- Asynchronous ingestion pipeline: upload API, job state machine, worker, retries and
  terminal failures, document status tracking.
- Hybrid retrieval (vector + BM25 + RRF fusion) with optional reranking.
- Answer pipeline with query rewrite, citation validation and refusal on weak evidence.
- Streaming chat over SSE and a no-build operations console with overview, lab,
  traces, jobs, storage and evaluation views.
- Multi-tenant isolation through PostgreSQL Row Level Security, tenant-scoped
  connections and per-document ACL tags.
- Object storage support (local filesystem or S3-compatible MinIO).
- Evaluation harness with a 50-case suite, refusal calibration and regression gate.
