# CLAUDE.md

给 AI agent 的项目约定。违反这些规则视为缺陷，不是风格问题。

## 项目是什么

企业级 RAG 知识库：Postgres + pgvector 存储，FastAPI 提供检索与流式问答。
迁移计划见 `docs/plans/2026-11-18-enterprise-kb-migration.md`（以它为准，不要自行改计划）。

## 命令

```powershell
py -3.13 -m pytest -q                      # 全量测试，改动后必须跑
py -3.13 -m pytest tests/test_xxx.py -q    # 单文件
py -3.13 scripts/run_eval.py --mock --out eval/current.json --compare eval/baseline.json
py -3.13 -m uvicorn app.main:app --port 8000
docker compose -f deploy/docker-compose.yml up -d
```

- **Python 一律用 `py -3.13`**（本机 `python` 是 2.7）。
- 数据库：容器 `rag-kb-pg`，宿主端口 **5433**，库/用户/密码均为 `kb`。
- 容器内 api 连库用 `db:5432`，不是 5433。

## 硬性规则

1. **禁止修改** `eval/baseline.json`、`eval/cases.jsonl`、`eval/demo_corpus.json`。
   基线是门禁的对照组；要改必须先说明原因并单独提交。
2. **禁止手动改表**。结构变更只能新增 `migrations/versions/000N_*.py`，`CREATE EXTENSION` 必须写在迁移里。
3. **租户隔离不可绕过**：任何读写 chunk 的路径都必须带 `tenant_id` + `kb_id`，缺失要抛 `MissingTenantFilterError`。
4. **不接受 body 里的 tenant_id**，只能来自鉴权依赖（`app/deps.py`）。
5. 不提交 `.env`、`data/`、`__pycache__`、`.pytest_cache/`。
6. 外部依赖（Embedding / LLM / Rerank）必须有降级路径，不允许让主流程 500。

## 目录

```
app/        FastAPI：routers / deps / services / schemas
src/        业务核心：chunk / store / store_pg / bm25 / bm25_store / retrieve / route / rerank / citation / guard
src/db/     SQLAlchemy 模型与引擎
migrations/ Alembic 迁移
eval/       评测集、语料、基线
scripts/    run_eval / ci_gate / migrate_json_to_pg / seed_bulk
tests/      契约测试 + 隔离测试 + 降级测试
```

## 验证顺序（每次改完）

1. `py -3.13 -m pytest -q` 全绿；
2. 涉及检索/切分的改动，额外跑 `run_eval.py --compare eval/baseline.json`，退化会返回 1；
3. 涉及接口的改动，起服务后 curl `/live`、`/ready`。

## 提交

- 小步提交，一次一件事；消息用 `feat(scope): …` / `fix(scope): …` / `test(scope): …` / `chore(scope): …`。
- 提交前确认 `git status` 里没有 `.env`、大文件、临时报告。
- 禁止 `git push`，除非明确要求。
