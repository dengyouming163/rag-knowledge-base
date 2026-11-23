# Deployment

Two deployment shapes are supported by the repository: a local Docker Compose stack for development and verification, and a single-host demo deployment behind an nginx gateway. Both use the same images, migrations and environment variables.

Default endpoints after a local start: API `http://127.0.0.1:8000`, console `http://127.0.0.1:8000/ui/`, OpenAPI `http://127.0.0.1:8000/docs`.

## Local Compose stack

Requirements: Docker Engine 26 or newer with Compose v2, and a shell at the repository root.

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml exec -T api alembic upgrade head
```

Services in `deploy/docker-compose.yml`:

| Service | Role | Published ports |
|---|---|---|
| `db` | PostgreSQL 16 + pgvector | `5433:5432` |
| `api` | FastAPI application and static console | `8000:8000` |
| `worker` | Single-replica ingestion worker (`scripts/run_worker.py --interval 5`) | none |
| `parser` | PDF parser, only with `--profile parser` | `8100:8000` |
| `minio` / `minio-init` | Optional S3-compatible storage and one-shot bucket/IAM setup | `9000`, `9001` |

Notes:

- The API and worker share the `uploads` named volume in `local` storage mode. In `s3` mode the volume is no longer required, because both processes read and write the same object keys.
- The `parser` service is behind a Compose profile so that a default `up` stays small. Start it with `docker compose -f deploy/docker-compose.yml --profile parser up -d parser`; the worker reaches it at `PARSER_BASE_URL=http://parser:8000`.
- `EMBED_MODE` is `mock` in the base Compose file so the stack starts without model keys. Set `EMBED_MODE=real` and `EMBED_API_KEY` for semantic embeddings; chat additionally needs `LLM_API_KEY` in the API container environment.

### Migrations

Migrations are run inside the API image so the application code and the Alembic environment always match:

```bash
docker compose -f deploy/docker-compose.yml exec -T api alembic upgrade head
```

To step back one revision (for example when a migration must be retried during development):

```bash
docker compose -f deploy/docker-compose.yml exec -T api alembic downgrade -1
```

`upgrade head` is idempotent; on a fresh database it creates the schema, the HNSW index, BM25 support tables, ACL and job columns, RLS on `chunks`, and the non-superuser role `kb_app`. The role is created by revision `0007_rls_chunks.py`.

## Environment variables

`.env.example` is the template. Values below are read by the API and worker processes; the Compose files map container names onto them.

| Variable | Used by | Default | Notes |
|---|---|---|---|
| `DB_HOST` | api, worker | `127.0.0.1` (host) / `db` (compose) | |
| `DB_PORT` | api, worker | `5433` (host) / `5432` (container) | Host port differs from the container port to avoid collisions. |
| `DB_USER` | api, worker | `kb` | Administrative/owner role used for migrations. |
| `DB_PASSWORD` | api, worker | `kb` | Override in any environment. |
| `DB_NAME` | api, worker | `kb` | |
| `DB_APP_USER` / `DB_APP_PASSWORD` | application connections | `kb_app` / `kb_app` | Non-superuser role; superusers bypass RLS. |
| `STORAGE_BACKEND` | api, worker | `local` | `local` writes under `data/uploads`; `s3` uses the `S3_*` settings. Unknown values raise an error rather than silently degrading. |
| `S3_ENDPOINT` | api, worker | `http://127.0.0.1:9000` (host) / `http://minio:9000` (compose) | Path-style addressing is mandatory for MinIO. |
| `S3_BUCKET` | api, worker | `rag-kb` | Required when `STORAGE_BACKEND=s3`. |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | api, worker | `rag-app` / `rag-app-secret` | Use the restricted application account created by `minio-init`, not root. |
| `S3_REGION` / `S3_USE_SSL` | api, worker | `us-east-1` / `false` | |
| `EMBED_MODE` | api, worker | `mock` (base compose), `real` (demo overlay) | `mock` produces 1024-dimension hash vectors with no semantics. |
| `EMBED_MODEL` | api, worker | `BAAI/bge-m3` | Stored per chunk as `embed_model_version`; changing it creates drift that must be re-embedded. |
| `EMBED_BASE_URL` / `EMBED_API_KEY` | api, worker | empty | Required for real embeddings. |
| `LLM_BASE_URL` / `LLM_MODEL` | api | `https://api.deepseek.com/v1` / `deepseek-chat` | |
| `LLM_API_KEY` | api | empty | Chat returns `503` when the chat client cannot be constructed. |
| `PARSER_BASE_URL` | api, worker | empty | Empty means local `pypdf` only; no OCR for scanned files. |
| `CORS_ALLOW_ORIGINS` | api | empty | Comma-separated allow-list; empty disables the CORS middleware entirely. |
| `MAX_UPLOAD_BYTES` | api | `52428800` (50 MB) | Enforced before any database or disk write; invalid values fall back to the default. Keep the nginx `client_max_body_size` at least as large. |
| `JOB_LEASE_SECONDS` | api, worker | `900` | Jobs stuck in `running` beyond this are reclaimed; non-positive or unparsable values fall back to `900`. |
| `OPS_ENABLE_ACTIONS` | api | `0` | `1` enables write actions (evaluation runs, manual retries). Admin role is still required. |
| `RERANK_BASE_URL` / `RERANK_API_KEY` / `RERANK_MODEL` | api | empty | Optional reranker; empty disables reranking. |
| `REFUSAL_MIN_SIMILARITY` | api | `0.56` | Cosine threshold for the refusal gate; `0` disables the gate. |
| `DEFAULT_TENANT` | api | `demo` | Used when `X-Tenant-Id` is absent. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | minio | `ragkb` / `ragkb-secret` | Initialization only. |

## Demo deployment

The demo is a single-host deployment: nginx terminates plain HTTP on `18000`, authenticates with Basic Auth, rate-limits per client IP, caps request bodies at 50 MB and proxies to `api:8000`. The API, database and MinIO ports are no longer published.

```bash
sudo ./scripts/deploy_demo.sh
```

The script is idempotent and performs, in order:

1. prerequisite checks: root, `docker`, Compose v2, port `18000` availability (unless the `ragkb-demo-*` stack already owns it), and at least 5 GB free on `/`;
2. `.env` generation when absent, with `umask 077` and random database, S3 and MinIO secrets; the file is forced to mode `600`;
3. Basic Auth credential generation into `deploy/nginx/htpasswd` (mode `600`) through an ephemeral `httpd` container;
4. `docker compose --env-file .env -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml up -d --build`;
5. database readiness, `alembic upgrade head` inside the API container, and `/ready` polling;
6. firewall opening for `18000/tcp` when `firewalld` is active;
7. health verification: gateway `/health` returns `200`, `/ui/` returns `401` without credentials, and an authenticated `GET /v1/storage` succeeds.

Compose **2.24 or newer** is required because the overlay removes published ports with `ports: !reset []`. Older versions reject or ignore the merge, which would leave the database, API and MinIO reachable directly. The overlay is `deploy/docker-compose.demo.yml`; the project name is pinned to `ragkb-demo`, which also fixes the volume names.

After deployment the entry point is:

`http://<demo-url>:18000/ui/`

Credentials are generated on the host in `.env` (`DEMO_USER` / `DEMO_PASSWORD`) and in `deploy/nginx/htpasswd`; they are not stored in the repository. Rotate them by regenerating the htpasswd file and recreating the `gateway` container.

### Gateway configuration

`deploy/nginx/demo.conf` defines the only public surface:

- `/health` is anonymous and proxies to the API `/live` probe, so the gateway health check does not depend on the database.
- `/ui/` and `/` (which covers `/v1`, `/live`, `/ready`, `/docs`, `/openapi.json`) require Basic Auth.
- `limit_req_zone ... rate=10r/s` with `burst=20 nodelay` and `limit_req_status 429` applies per client IP.
- `client_max_body_size 50m` mirrors the application upload limit.
- `proxy_read_timeout 300s` and `proxy_buffering off` keep SSE chat responses streaming.

## Backup and rollback

### Database

Back up before any migration or image change:

```bash
docker compose -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml \
  exec -T db pg_dump -U kb -d kb -Fc > ragkb-$(date +%Y%m%d).dump
```

Restore into a running database:

```bash
docker compose -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml \
  exec -T db pg_restore -U kb -d kb --clean --if-exists < ragkb-YYYYMMDD.dump
```

### Objects

Uploaded files live in the `ragkb-demo_uploads` volume in `local` mode and in the `ragkb-demo_miniodata` volume for the bundled MinIO. Archive both volumes alongside the database dump:

```bash
for volume in ragkb-demo_uploads ragkb-demo_miniodata; do
  docker run --rm -v "${volume}:/data:ro" -v "$PWD:/backup" alpine \
    tar czf "/backup/${volume}-$(date +%Y%m%d).tgz" -C /data .
done
```

Restore by extracting into the empty volumes before starting the stack. When an external S3 bucket is used, snapshot it with the provider's versioning or replication mechanism instead.

### Application rollback

Compose builds the `api` and `worker` images from the current checkout. Tag the known-good images before an upgrade:

```bash
docker tag ragkb-demo-api ragkb-demo-api:backup-$(date +%Y%m%d)
docker tag ragkb-demo-worker ragkb-demo-worker:backup-$(date +%Y%m%d)
```

To roll back, retag the backup image as the working tag and recreate the services:

```bash
docker tag ragkb-demo-api:backup-YYYYMMDD ragkb-demo-api
docker tag ragkb-demo-worker:backup-YYYYMMDD ragkb-demo-worker
docker compose -p ragkb-demo -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml up -d
```

Database rollback is separate: prefer restoring the dump. If the schema must move back one revision, run `alembic downgrade -1` inside the API container while the previous application image is running, then verify `/ready` and a job round-trip.

### Verification after deploy or rollback

```bash
curl -fsS http://127.0.0.1:18000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18000/ui/
curl -fsS -u "$DEMO_USER:$DEMO_PASSWORD" -H 'X-Tenant-Id: demo' http://127.0.0.1:18000/v1/storage
```

Expected: `/health` returns `200`, `/ui/` returns `401` without credentials, and the authenticated storage call returns a JSON document list.
