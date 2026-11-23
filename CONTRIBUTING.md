# Contributing

Thanks for taking the time to improve this project. The workflow below keeps the
test suite, the evaluation baseline and the API contract consistent.

## Development setup

Requirements: Python 3.13, PostgreSQL 16 with the `pgvector` extension, and Node 20+
for the front-end unit tests.

```powershell
py -3.13 -m pip install -e ".[test,dev]"
docker compose -f deploy/docker-compose.yml up -d db
alembic upgrade head
```

Create `.env` from `.env.example` and fill in the model keys. Without keys the stack
falls back to hash embeddings and refuses to answer questions, which is enough for
most unit tests but not for evaluation.

## Checks before opening a pull request

```powershell
py -3.13 -m pytest -q
py -3.13 -m ruff check .
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs tests/js/storage.test.mjs
```

The Node runner must be given explicit file names: the console has no bundler and
`node --test tests/js` would not resolve the module graph the same way.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(scope): ...`,
`fix(scope): ...`, `docs: ...`, `chore: ...`, `test: ...`. Keep one logical change per
commit and describe the reason in the body when the diff is not self-explanatory.

## Pull requests

- Add or update tests for behavior changes; bug fixes should include a regression test.
- Update `docs/` when an endpoint, environment variable or operational step changes.
- Do not commit `.env`, `data/`, `deploy/nginx/htpasswd`, traces or uploaded documents.
- Never weaken tenant isolation: every query touching tenant data must carry the
  tenant filter, and `chunks` access must go through `tenant_scope`.
