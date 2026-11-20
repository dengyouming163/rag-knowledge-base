# 降级演练 Runbook

## 适用范围、前置条件与风险

适用于本仓库的本地 Docker Compose 环境，在仓库根目录用 PowerShell 执行。API 为 `http://127.0.0.1:8000`，PostgreSQL 宿主端口为 `5433`；parser 部署后通常使用容器内地址 `http://parser:8000`、宿主地址 `http://127.0.0.1:8100`。

本 runbook 不自带 parser 部署或 PDF 样本。**干净检出必须先确认依赖已经部署**：

- `src.parsers.pdf.PdfParserClient`、`app.deps.get_embedder`、Python 3.13、`httpx` 与 `pypdf` 必须可用。
- parser 演练前执行 `docker compose --profile parser -f deploy/docker-compose.yml config --services`，输出必须包含 `parser`。若不包含，先在包含 parser 部署变更的工作树或目标环境完成部署；不要直接执行后续命令。profile 名称和宿主端口也应按实际 Compose 配置调整。
- 本次使用当前工作区的 `eval/fixtures/pdf/policy_single.pdf`，该文件是未跟踪样本，不随本 runbook 提交。其他环境必须把 `$PdfPath` 改为已有的文字型 PDF，并把 `$PdfKeyword` 改为该文件确实包含的文本；不能用扫描件代替，也不会触发 OCR 或模型下载。
- 不假定 `src/__init__.py` 会自动加载 `.env`。HEAD 不加载，但其他工作树可能包含此类变更；Embedding 命令统一在导入 `app.deps` 后清理独立子进程环境，在两种行为下都能验证无 Key 降级。

仅在已批准的演练窗口执行。停 parser 时上传链路会退回本地 `pypdf`；停 DB 时读写和任务处理不可用，worker 可能记录连接错误并由既有 `restart: unless-stopped` 策略重启。只用 `stop` / `start`，禁止 `down -v`、删除容器或卷，不要为演练停止 worker。`/ready` 有 5 秒缓存，故障前后都不能把缓存结果当作实时探测。

## 通用前置检查

```powershell
# Windows PowerShell 向原生进程管道发送中文 here-string 前，先使用 UTF-8。
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
docker compose -f deploy/docker-compose.yml ps -a
Invoke-WebRequest -Uri http://127.0.0.1:8000/ready -UseBasicParsing
py -3.13 -c "import pypdf,httpx; print('pypdf='+pypdf.__version__); print('httpx='+httpx.__version__)"
```

继续前应确认 api/db 为 `Up ... (healthy)`、worker 为 `Up`，`/ready` 返回 200 且正文为 `{"ok":true,"db":"up"}`；parser 演练还要求 parser 为 `healthy`。记录原始状态，结束后恢复本次改变的服务。

本次修正重跑的前置检查窗口：**2026-11-20 09:09:12—09:09:33 +08:00**。实际 `config --services`（启用 parser profile）为 `db/parser/worker/api`，api/parser/db 均 `Up ... (healthy)`、worker `Up`，`/ready` 与 parser `/health` 均为 200；本机 `pypdf=7.1.0`、`httpx=0.28.1`。

## 1. parser 停止：远端失败退回本地 pypdf

下面命令先检查 parser 是否存在于当前 Compose 配置，再接收 PDF 路径和关键字。Python 编排包含真实 HTTP 异常与本地解析入口追踪，并在 `finally` 中启动 parser、轮询 Docker 健康和 HTTP `/health`；最多尝试 38 次（单次 HTTP 超时 2 秒），未恢复会明确抛错。

```powershell
$services = docker compose --profile parser -f deploy/docker-compose.yml config --services
$services
if ($services -notcontains 'parser') { throw '当前 Compose 未部署 parser，请先完成 parser 部署。' }
$PdfPath = 'eval/fixtures/pdf/policy_single.pdf'  # 本次工作区样本；其他环境必须修改
$PdfKeyword = '七天无理由退款'                  # 随 PDF 修改
if (-not (Test-Path -LiteralPath $PdfPath)) { throw "文字型 PDF 不存在：$PdfPath" }
@'
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pypdf
from src.parsers.pdf import PdfParserClient

ROOT = Path.cwd()
URL = "http://127.0.0.1:8100"
PATH = Path(sys.argv[1])
KEYWORD = sys.argv[2]
COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]


def compose(action):
    result = subprocess.run(
        [*COMPOSE, action, "parser"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"compose {action} parser failed: {result.stderr.strip()}")
    print(f"compose_{action}_parser=ok", flush=True)


def inspect(format_string):
    return subprocess.check_output(
        ["docker", "inspect", "--format", format_string, "rag-kb-parser"],
        text=True, encoding="utf-8", errors="replace",
    ).strip()


def wait_parser():
    deadline = time.monotonic() + 75
    last = "not checked"
    for attempt in range(1, 39):
        try:
            health = inspect("{{.State.Health.Status}}")
            response = httpx.get(f"{URL}/health", timeout=2)
            last = f"docker_health={health}, http={response.status_code}, body={response.text}"
            if health == "healthy" and response.status_code == 200:
                print(f"parser_recovery={last}, attempts={attempt}", flush=True)
                return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise RuntimeError(f"parser recovery timed out after 75s: {last}")


class TraceTransport(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request):
        try:
            return self.inner.handle_request(request)
        except httpx.HTTPError as exc:
            print(f"remote_exception={type(exc).__name__}: {exc}", flush=True)
            raise

    def close(self):
        self.inner.close()


original_local = PdfParserClient._local
original_reader = pypdf.PdfReader


def traced_local(content, *, reason="", retryable=None):
    print(f"local_fallback_reason={reason}; retryable={retryable}", flush=True)
    return original_local(content, reason=reason, retryable=retryable)


def traced_reader(*args, **kwargs):
    print("local_source=pypdf.PdfReader", flush=True)
    return original_reader(*args, **kwargs)


print(f"started_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
print(f"parser_url={URL}; fixture={PATH.as_posix()}; bytes={PATH.stat().st_size}")
initial_health = inspect("{{.State.Health.Status}}")
print(f"parser_health_before={initial_health}")
if initial_health != "healthy":
    raise RuntimeError(f"parser 初始状态不是 healthy：{initial_health}")

try:
    compose("stop")
    print(f"parser_state_during={inspect('{{.State.Status}}')}")
    PdfParserClient._local = staticmethod(traced_local)
    pypdf.PdfReader = traced_reader
    with PdfParserClient(URL, timeout=3, transport=TraceTransport()) as client:
        blocks = client.parse(PATH.read_bytes(), filename=PATH.name)
    text = "\n".join(block.text for block in blocks)
    print(f"local_blocks={len(blocks)}; pages={[b.page for b in blocks]}; types={[b.type for b in blocks]}")
    print(f"text_chars={len(text)}; text_summary={text[:180].replace(chr(10), ' | ')}")
    assert blocks and KEYWORD in text
finally:
    pypdf.PdfReader = original_reader
    PdfParserClient._local = staticmethod(original_local)
    compose("start")
    wait_parser()

print(f"finished_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
'@ | py -3.13 -X utf8 - $PdfPath $PdfKeyword
```

预期远端为 `httpx.ConnectError`，随后实际进入 `pypdf.PdfReader` 并返回文本块；恢复输出必须同时包含 `docker_health=healthy` 和 `http=200`。若 PDF 没有文字层，本地路径不会代替 OCR，而会抛出 `ParserUnavailable`。

本次按上方命令重跑，以下为其 stdout/stderr 中除 PowerShell 服务列表外的连续采集输出：

```text
started_at=2026-11-20T09:12:13+08:00
parser_url=http://127.0.0.1:8100; fixture=eval/fixtures/pdf/policy_single.pdf; bytes=21617
parser_health_before=healthy
compose_stop_parser=ok
parser_state_during=exited
remote_exception=ConnectError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
local_fallback_reason=解析服务不可用：[WinError 10061] 由于目标计算机积极拒绝，无法连接。; retryable=True
local_source=pypdf.PdfReader
local_blocks=1; pages=[1]; types=['text']
text_chars=150; text_summary=售后政策 | 一、退款 | 本文说明售后政策中的退款与换货规则，适用于全渠道订单。 | 支持七天无理由退款。退款审核通过后按原支付路径退回，通常 1-3 个工作日到账。 | 二、换货 | 收到货七天内可申请换货。换货需要商品完好、配件齐全，包装不得破损。 | 以下情况不支持无理由退款：定制类商品、已激活的软件、拆封的耗材。
compose_start_parser=ok
parser_recovery=docker_health=healthy, http=200, body={"status":"ok"}, attempts=14
finished_at=2026-11-20T09:12:52+08:00
```

## 2. 移除 EMBED_API_KEY：使用确定性 HashEmbeddingClient

此验证只修改独立子进程环境，不改父进程、系统环境、`.env` 或 Compose 配置，也不打印 Key。导入后再移除 `EMBED_API_KEY`，避免任何工作树可能存在的自动加载行为影响结果；同时移除 `EMBED_MODE`，排除显式 mock 配置。

```powershell
@'
import hashlib
import json
import os
from datetime import datetime
from app import deps

os.environ.pop("EMBED_API_KEY", None)
os.environ.pop("EMBED_MODE", None)
print(f"started_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
print(f"pid={os.getpid()}; EMBED_API_KEY_present={'EMBED_API_KEY' in os.environ}; EMBED_MODE={os.environ.get('EMBED_MODE', '<unset>')}")
deps.get_embedder.cache_clear()
embedder = deps.get_embedder()
client_type = f"{type(embedder).__module__}.{type(embedder).__qualname__}"
print(f"client_type={client_type}; dim={embedder.dim}")
print(f"factory_cached_same_object={deps.get_embedder() is embedder}")
vector = embedder.embed_texts(["售后退款政策"])[0]
deps.get_embedder.cache_clear()
rebuilt = deps.get_embedder()
rebuilt_vector = rebuilt.embed_texts(["售后退款政策"])[0]
print(f"rebuilt_type={type(rebuilt).__module__}.{type(rebuilt).__qualname__}; rebuilt_is_new={rebuilt is not embedder}")
print(f"vector_length={len(vector)}; deterministic_after_rebuild={vector == rebuilt_vector}")
print(f"nonzero_count={sum(v != 0 for v in vector)}; vector_sha256={hashlib.sha256(json.dumps(vector).encode()).hexdigest()}")
assert client_type == "scripts.compare_routes.HashEmbeddingClient"
assert embedder.dim == len(vector) == 1024
assert vector == rebuilt_vector and any(vector)
print(f"finished_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
'@ | py -3.13 -X utf8 -
```

预期类型为 `scripts.compare_routes.HashEmbeddingClient`，维度为 1024，同一文本重建前后向量一致。哈希向量只保证链路可用，不代表真实语义检索质量。

本验证在新子进程生效，**无需重启主服务**。`get_embedder` 有进程内 `lru_cache`：在同一维护进程内已解析过 embedder 时，须先执行 `get_embedder.cache_clear()`，下次依赖解析才会新建对象；另一个 CLI 进程清缓存不会影响运行中的 API，已持有的对象引用也不会被替换。当前没有清缓存 HTTP 接口，API/worker 如需应用新配置，应重建相应进程；若修改了 Compose 环境，使用 `docker compose -f deploy/docker-compose.yml up -d --force-recreate api`（worker 也需要时同理指定 `worker`），而不是仅用不会应用新容器环境的 `restart`。

本次按上方命令重跑的完整 stdout：

```text
started_at=2026-11-20T09:13:16+08:00
pid=14784; EMBED_API_KEY_present=False; EMBED_MODE=<unset>
client_type=scripts.compare_routes.HashEmbeddingClient; dim=1024
factory_cached_same_object=True
rebuilt_type=scripts.compare_routes.HashEmbeddingClient; rebuilt_is_new=True
vector_length=1024; deterministic_after_rebuild=True
nonzero_count=11; vector_sha256=d7cd82ea0d74cc639136fd4869791a395fa249cb47e7015bc1ca446689e70e1f
finished_at=2026-11-20T09:13:17+08:00
```

## 3. DB 停止：`/ready` 返回 503

下面命令先取得 200，实际等待 6 秒让缓存过期，再停 DB。HTTP 检查位于 `try`，启动和恢复轮询位于 `finally`；最多尝试 45 次（单次 HTTP 超时 10 秒），DB 健康与 `/ready` 未同时恢复会明确抛错。故障请求结束后立即启动 DB，不做额外停留。

```powershell
@'
import subprocess
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path.cwd()
READY_URL = "http://127.0.0.1:8000/ready"
COMPOSE = ["docker", "compose", "-f", "deploy/docker-compose.yml"]


def compose(action):
    result = subprocess.run(
        [*COMPOSE, action, "db"], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"compose {action} db failed: {result.stderr.strip()}")
    print(f"compose_{action}_db=ok", flush=True)


def inspect(container, format_string):
    return subprocess.check_output(
        ["docker", "inspect", "--format", format_string, container],
        text=True, encoding="utf-8", errors="replace",
    ).strip()


def wait_recovery():
    deadline = time.monotonic() + 90
    last = "not checked"
    for attempt in range(1, 46):
        try:
            health = inspect("rag-kb-pg", "{{.State.Health.Status}}")
            response = httpx.get(READY_URL, timeout=10)
            last = f"docker_health={health}, ready_http={response.status_code}, body={response.text}"
            if health == "healthy" and response.status_code == 200:
                print(f"db_recovery={last}, attempts={attempt}", flush=True)
                return
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise RuntimeError(f"DB/API recovery timed out after 90s: {last}")


print(f"started_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
print(f"worker_state_before={inspect('rag-kb-worker', '{{.State.Status}}')}; db_health_before={inspect('rag-kb-pg', '{{.State.Health.Status}}')}")
before = httpx.get(READY_URL, timeout=10)
print(f"ready_before_http={before.status_code}; body={before.text}")
assert before.status_code == 200 and before.json() == {"ok": True, "db": "up"}
print("cache_wait_seconds=6.0", flush=True)
time.sleep(6.0)

try:
    compose("stop")
    print(f"db_state_during={inspect('rag-kb-pg', '{{.State.Status}}')}")
    during = httpx.get(READY_URL, timeout=10)
    body = during.json()
    print(f"ready_during_http={during.status_code}; body={during.text}; error_type={body.get('db')}")
    assert during.status_code == 503 and body == {"ok": False, "db": "down"}
finally:
    compose("start")
    wait_recovery()

print(f"worker_state_after={inspect('rag-kb-worker', '{{.State.Status}}')}; db_health_after={inspect('rag-kb-pg', '{{.State.Health.Status}}')}")
print(f"finished_at={datetime.now().astimezone().isoformat(timespec='seconds')}")
'@ | py -3.13 -X utf8 -
```

预期故障时 HTTP 503、正文 `{"ok":false,"db":"down"}`；恢复输出必须同时包含 `docker_health=healthy` 和 `ready_http=200`。worker 在故障窗口出现 `sqlalchemy.exc.OperationalError` 或被既有策略重启都不能单独作为恢复证据，必须结合最终容器状态和 `/ready`。

本次按上方命令重跑的完整 stdout：

```text
started_at=2026-11-20T09:13:44+08:00
worker_state_before=running; db_health_before=healthy
ready_before_http=200; body={"ok":true,"db":"up"}
cache_wait_seconds=6.0
compose_stop_db=ok
db_state_during=exited
ready_during_http=503; body={"ok":false,"db":"down"}; error_type=down
compose_start_db=ok
db_recovery=docker_health=healthy, ready_http=200, body={"ok":true,"db":"up"}, attempts=3
worker_state_after=running; db_health_after=healthy
finished_at=2026-11-20T09:14:05+08:00
```

## 完成后检查

```powershell
docker compose -f deploy/docker-compose.yml ps -a
Invoke-WebRequest -Uri http://127.0.0.1:8000/ready -UseBasicParsing
Invoke-WebRequest -Uri http://127.0.0.1:8100/health -UseBasicParsing
py -3.13 -m pytest -q
```

本次修正重跑后的非破坏性复核时间：**2026-11-20 09:16:16 +08:00**。实际状态：api `Up 10 hours (healthy)`、parser `Up 3 minutes (healthy)`、DB `Up 2 minutes (healthy)`、worker `Up 2 minutes`；`/ready` 为 `200 {"ok":true,"db":"up"}`，parser `/health` 为 `200 {"status":"ok"}`。

全量回归实测：`383 passed, 1 warning in 25.39s`，退出码 0；唯一警告为 Alembic `prepend_sys_path` 的 `DeprecationWarning`，无失败或跳过。
