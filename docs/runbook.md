# 降级演练 Runbook

## 适用范围与风险

适用于本仓库的本地 Docker Compose 环境，在仓库根目录用 PowerShell 执行。Compose 文件为 `deploy/docker-compose.yml`：API 为 `http://127.0.0.1:8000`，parser 容器内地址为 `http://parser:8000`、宿主地址为 `http://127.0.0.1:8100`，PostgreSQL 宿主端口为 `5433`。

- 仅在已批准的演练窗口执行。停 parser 时上传链路会退回本地 `pypdf`；停 DB 时读写和任务处理不可用，worker 可能记录连接错误并由既有 `restart: unless-stopped` 策略重启。
- 只用 `stop` / `start`，禁止 `down -v`、删除容器或卷。不要为演练停止 worker；结束后必须恢复本次改变的服务，并核对 DB 与 `/ready`。
- `/ready` 结果缓存 5 秒。停 DB 前先让上一次 200 缓存过期，否则可能误读为故障未生效；恢复后稍等再判断。
- 以下 parser、DB 步骤假定二者原本在运行。若 parser 原本未运行，可用 `docker compose -f deploy/docker-compose.yml --profile parser up -d parser` 启动后演练，但最后应再次 `stop parser` 恢复原状。
- Embedding 验证只改独立子进程环境，不改系统环境、`.env` 或 Compose 配置，也不要打印任何 Key。

## 前置检查

```powershell
# Windows PowerShell 向原生进程管道发送中文 here-string 前，先使用 UTF-8。
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
docker compose -f deploy/docker-compose.yml ps -a
Invoke-WebRequest -Uri http://127.0.0.1:8000/ready -UseBasicParsing
Invoke-WebRequest -Uri http://127.0.0.1:8100/health -UseBasicParsing
py -3.13 -c "import pypdf, httpx; print('pypdf=' + pypdf.__version__); print('httpx=' + httpx.__version__)"
```

继续前应确认 `api`、`parser`、`db` 为 `Up ... (healthy)`，`worker` 为 `Up`，`/ready` 返回 200 且正文为 `{"ok":true,"db":"up"}`。记录原始状态，演练后按原状态恢复。

本次前置检查时间：**2026-11-20 08:41:11 +08:00**。实际状态为 api/parser/db 均 `Up ... (healthy)`、worker `Up`，`/ready` 为 200，parser `/health` 为 200；本机 `pypdf=7.1.0`、`httpx=0.28.1`。

## 1. parser 停止：远端失败退回本地 pypdf

使用仓库已有的文字型 PDF `eval/fixtures/pdf/policy_single.pdf`，不触发 OCR 或模型下载。以下传输层只记录真实 HTTP 异常，`PdfReader` 包装只记录本地解析入口，不改变解析结果。

```powershell
try {
  docker compose -f deploy/docker-compose.yml stop parser
  @'
import httpx
import pypdf
from pathlib import Path
from src.parsers.pdf import PdfParserClient

url = "http://127.0.0.1:8100"
path = Path("eval/fixtures/pdf/policy_single.pdf")

class TraceTransport(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()
    def handle_request(self, request):
        try:
            return self.inner.handle_request(request)
        except httpx.HTTPError as exc:
            print(f"remote_exception={type(exc).__name__}: {exc}")
            raise
    def close(self):
        self.inner.close()

original_local = PdfParserClient._local
def traced_local(content, *, reason="", retryable=None):
    print(f"local_fallback_reason={reason}; retryable={retryable}")
    return original_local(content, reason=reason, retryable=retryable)

original_reader = pypdf.PdfReader
def traced_reader(*args, **kwargs):
    print("local_source=pypdf.PdfReader")
    return original_reader(*args, **kwargs)

PdfParserClient._local = staticmethod(traced_local)
pypdf.PdfReader = traced_reader
print(f"parser_url={url}; fixture={path}; bytes={path.stat().st_size}")
with PdfParserClient(url, timeout=3, transport=TraceTransport()) as client:
    blocks = client.parse(path.read_bytes(), filename=path.name)
text = "\n".join(block.text for block in blocks)
print(f"local_blocks={len(blocks)}; pages={[b.page for b in blocks]}; text_chars={len(text)}")
print(text[:260])
assert blocks and "七天无理由退款" in text
'@ | py -3.13 -X utf8 -
} finally {
  docker compose -f deploy/docker-compose.yml start parser
}

# 健康检查 interval 为 30 秒，稍等后核对，不要把 health: starting 误判为失败。
docker compose -f deploy/docker-compose.yml ps -a parser
Invoke-WebRequest -Uri http://127.0.0.1:8100/health -UseBasicParsing
```

预期远端为 `httpx.ConnectError`，随后实际进入 `pypdf.PdfReader` 并返回文本块；恢复后 parser 为 `healthy`，`/health` 返回 `{"status":"ok"}`。若 PDF 没有文字层，本地路径不会代替 OCR，而会抛出 `ParserUnavailable`。

本次实测 **2026-11-20 08:43:11—08:43:48 +08:00**，关键原始输出：

```text
rag-kb-parser  Exited (0) 1 second ago
parser_url=http://127.0.0.1:8100 (compose internal http://parser:8000)
fixture=eval/fixtures/pdf/policy_single.pdf bytes=21617
remote_exception=ConnectError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
local_fallback_reason=解析服务不可用：[WinError 10061] 由于目标计算机积极拒绝，无法连接。; retryable=True
local_source=pypdf.PdfReader
local_blocks=1 pages=[1] types=['text']
text_chars=150 text_summary=售后政策 | 一、退款 | 本文说明售后政策中的退款与换货规则，适用于全渠道订单。 | 支持七天无理由退款。...
recovery=http=200, docker_health=healthy, body={"status":"ok"}
rag-kb-parser  Up 32 seconds (healthy)
```

## 2. 移除 EMBED_API_KEY：使用确定性 HashEmbeddingClient

`src/__init__.py` 会在导入时加载 `.env`，所以必须在导入 `app.deps` 后再清理该子进程环境。这里同时移除 `EMBED_MODE`，排除显式 mock 配置对验证结果的影响；父进程和系统环境均不变。

```powershell
@'
import hashlib
import json
import os
from app import deps

os.environ.pop("EMBED_API_KEY", None)
os.environ.pop("EMBED_MODE", None)
print(f"EMBED_API_KEY_present={'EMBED_API_KEY' in os.environ}")
deps.get_embedder.cache_clear()  # 仅限这个独立子进程
embedder = deps.get_embedder()
vector = embedder.embed_texts(["售后退款政策"])[0]
deps.get_embedder.cache_clear()
rebuilt = deps.get_embedder()
rebuilt_vector = rebuilt.embed_texts(["售后退款政策"])[0]
print(f"type={type(embedder).__module__}.{type(embedder).__qualname__}; dim={embedder.dim}")
print(f"length={len(vector)}; nonzero={sum(v != 0 for v in vector)}")
print(f"rebuilt_is_new={rebuilt is not embedder}; deterministic={vector == rebuilt_vector}")
print(f"sha256={hashlib.sha256(json.dumps(vector).encode()).hexdigest()}")
assert embedder.dim == len(vector) == 1024
assert vector == rebuilt_vector and any(vector)
'@ | py -3.13 -X utf8 -
```

预期类型为 `scripts.compare_routes.HashEmbeddingClient`，维度为 1024，同一文本重建前后向量一致。哈希向量只保证链路可用，不代表真实语义检索质量。

本验证在新子进程生效，**无需重启主服务**。`get_embedder` 有进程内 `lru_cache`：已在同一维护进程内解析过 embedder 时，须先执行 `get_embedder.cache_clear()`，下次依赖解析才会新建对象；在另一个 CLI 进程清缓存不会影响运行中的 API，已持有的对象引用也不会被替换。当前没有清缓存 HTTP 接口，API/worker 如需应用新配置，应重建相应进程；若修改了 Compose 环境，使用 `docker compose -f deploy/docker-compose.yml up -d --force-recreate api`（worker 也需要时同理指定 `worker`），而不是仅用不会应用新容器环境的 `restart`。

本次实测 **2026-11-20 08:45:40—08:45:41 +08:00**，关键原始输出：

```text
pid=8796 EMBED_API_KEY_present=False EMBED_MODE=<unset>
client_type=scripts.compare_routes.HashEmbeddingClient dim=1024
factory_cached_same_object=True
rebuilt_type=scripts.compare_routes.HashEmbeddingClient rebuilt_is_new=True
vector_length=1024 deterministic_after_rebuild=True
nonzero_count=11 vector_sha256=d7cd82ea0d74cc639136fd4869791a395fa249cb47e7015bc1ca446689e70e1f
```

常见误判：若在导入前 `unset`，`.env` 可能再次填入 Key，得到真实 `EmbeddingClient`；应检查清理顺序，而不是把该结果当作 mock 已生效。

## 3. DB 停止：`/ready` 返回 503

先取得 200 并等待 5 秒以上，再停 DB。HTTP 检查放在 `try` 中，启动恢复放在 `finally` 中，即使断言或请求失败也会执行恢复。

```powershell
py -3.13 -c "import httpx; r=httpx.get('http://127.0.0.1:8000/ready'); print(r.status_code, r.text); assert r.status_code == 200"
Start-Sleep -Seconds 6
try {
  docker compose -f deploy/docker-compose.yml stop db
  @'
import httpx
r = httpx.get("http://127.0.0.1:8000/ready", timeout=10)
print(f"status={r.status_code}; body={r.text}; error_type={r.json().get('db')}")
assert r.status_code == 503 and r.json() == {"ok": False, "db": "down"}
'@ | py -3.13 -X utf8 -
} finally {
  docker compose -f deploy/docker-compose.yml start db
}

# 等待容器健康，并让可能存在的 503 负缓存过期。
do {
  Start-Sleep -Seconds 2
  $dbHealth = docker inspect --format '{{.State.Health.Status}}' rag-kb-pg
  Write-Output "db_health=$dbHealth"
} until ($dbHealth -eq 'healthy')
Start-Sleep -Seconds 6
Invoke-WebRequest -Uri http://127.0.0.1:8000/ready -UseBasicParsing
docker compose -f deploy/docker-compose.yml ps -a
```

预期故障时 HTTP 503、正文 `{"ok":false,"db":"down"}`；恢复后 DB 为 `healthy`，`/ready` 为 200、正文 `{"ok":true,"db":"up"}`，api/parser 仍健康，worker 回到 running。worker 在 DB 停止或重新启动期间出现 `sqlalchemy.exc.OperationalError` 是本次故障窗口内的已知现象，不能只看一条日志就宣布恢复，必须结合最终容器状态和 `/ready`。

本次实测 **2026-11-20 08:46:39—08:47:04 +08:00**，关键原始输出：

```text
worker_state_before=running db_health_before=healthy
ready_before_http=200 body={"ok":true,"db":"up"}
cache_wait_seconds=5.2
rag-kb-pg  Exited (0) 1 second ago
ready_during_http=503 body={"ok":false,"db":"down"} error_type=down
restoring_db=true
recovery=docker_health=healthy, ready_http=200, body={"ok":true,"db":"up"}
worker_state_after=running db_health_after=healthy
rag-kb-api     Up 9 hours (healthy)
rag-kb-pg      Up 8 seconds (healthy)
rag-kb-worker  Up 3 seconds
```

worker 的 `Up 3 seconds` 表明故障期间既有重启策略曾拉起 worker；本次未停止或杀死 worker。恢复后再次检查，worker 持续 running，DB/API/parser 健康，两个 HTTP 探针均为 200。

## 完成后检查

```powershell
docker compose -f deploy/docker-compose.yml ps -a
Invoke-WebRequest -Uri http://127.0.0.1:8000/ready -UseBasicParsing
Invoke-WebRequest -Uri http://127.0.0.1:8100/health -UseBasicParsing
py -3.13 -m pytest -q
```

本次三项演练后的服务复核：api/parser/db 均 `Up ... (healthy)`，worker 持续 `Up`，`/ready` 为 `200 {"ok":true,"db":"up"}`，parser `/health` 为 `200 {"status":"ok"}`。

全量回归实测：`383 passed, 1 warning in 24.93s`，退出码 0；唯一警告为 Alembic `prepend_sys_path` 的 `DeprecationWarning`，无失败或跳过。
