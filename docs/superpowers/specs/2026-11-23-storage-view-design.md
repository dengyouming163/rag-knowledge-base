# 工作台「存储」视图设计

- 日期：2026-11-23
- 状态：已确认，待实施
- 影响范围：上传接口（写 meta）、新增 `GET /v1/storage`、前端新增视图

## 1. 背景与目标

对象存储（MinIO / 本地磁盘）已接入入库管线，但工作台没有任何地方能看到「当前存了什么、存了多少、哪些入库成功/失败」。运维排查只能去数据库或 MinIO 控制台翻。

目标：在工作台新增一个独立的「存储」视图，只读地展示当前租户下已上传文档的清单与存储用量，让上传结果可观测。

## 2. 范围

### 做

- 左侧导航新增「存储」入口，独立视图
- 顶部状态卡：后端类型、桶名、文档总数、总字节数、各状态计数
- 过滤栏：按 KB、按状态过滤
- 文档表格：文件名、KB、大小、状态、上传时间
- 新增 `GET /v1/storage` 端点，数据来自 `documents` 表
- 上传时把原始文件名与文件大小写入 `documents.meta`，供视图展示
- local 与 s3 两种后端下行为一致
- 中英文案

### 不做

- 不在本视图上传、删除、下载文件（上传在「实验室」；删除走现有接口，避免「删存储留 DB」不一致）
- 不列存储里的原始对象 / 不做孤立对象诊断（不扩展 `Storage` 协议的 `list`）
- 不做分页游标（首次 limit 截断，后续按需扩展）
- 不加跨租户视图（严格按请求头租户隔离）
- 不要求管理员权限（与入库/文档视图同级，后续接入权限系统再收口）
- 不回填历史数据（旧行 meta 为空时按降级规则显示）

## 3. UI 设计

### 3.1 导航

`app/static/index.html` 左侧导航在「审计」之后新增一项（序号 08）：

```html
<li><button class="wb-nav-link" type="button" data-nav="storage" aria-controls="view-storage"><span class="wb-nav-index">08</span><span data-i18n="nav.storage">Storage</span></button></li>
```

并新增对应视图容器 `<section id="view-storage" data-view="storage" hidden aria-labelledby="view-storage-title">…</section>`，结构与 `view-audit` 一致（kicker + 标题 + 内容区）。

### 3.2 视图内容

1. **状态卡**（横向一排小卡片）：后端（S3 / Local）、桶（s3 显示桶名，local 显示「—」）、文档总数、总大小（人类可读，如 42 MB）、就绪数。
2. **过滤栏**：KB 下拉（从返回的 documents 里聚合去重，「全部」为默认）、状态下拉（全部 / 就绪 / 入库中 / 失败）。
3. **文档表格**：列 = 文件名、KB、大小、状态、上传时间。
   - 状态用色标：ready→绿、pending→黄、failed→红；未知状态原样显示、无色标。
   - 大小：字节数格式化（B/KB/MB/GB）；缺失显示「—」。
   - 上传时间：相对或 `MM-DD HH:mm`。
4. **空态 / 加载态 / 错误态**：与其他视图一致（复用 `formatError`、i18n）。
5. 顶部一行提示「只读视图 · 上传请去『实验室』」。

刷新机制：复用 `workbuddy:refresh` 与 `workbuddy:scopechange` 事件；进入视图（`setActive(true)`）时若尚未加载则拉一次。作用域切换（租户/KB）时清掉旧数据并重拉。

## 4. 数据来源与缺口修复

### 4.1 现状缺口

`documents` 表（`src/db/models.py:68`）字段为 `id, tenant_id, kb_id, title, source_url, content_hash, status, meta, created_at, updated_at`，**没有 filename 或 size 列**。上传时（`app/routers/documents.py:92`）`title` 被写成 doc_id 占位、`meta` 写死 `'{}'::jsonb`，worker 与入库流程从不回填 `meta`。因此视图所需的「原始文件名」和「文件大小」目前在 DB 里不存在。

### 4.2 修复：上传时写 meta

在 `app/routers/documents.py` 的 INSERT 把 `meta` 从 `'{}'::jsonb` 改为 `CAST(:meta AS jsonb)`，参数：

```python
import json
meta = json.dumps({"filename": file.filename or "", "size": len(content)}, ensure_ascii=False)
```

INSERT 列表与参数相应加入 `meta` / `:meta`。其余上传逻辑（去重、落盘、入队）不变。

**降级规则**（视图读取时）：
- `filename`：`meta.filename` → 否则取 `source_url` 的 basename → 否则用 `id`。
- `size`：`meta.size`（int）→ 否则 `null`，前端显示「—」。

连接器入库（`src/ingest_jobs.py:99`）也会 INSERT documents，但那里没有上传文件名/大小，保持 `meta='{}'`，视图按降级显示。历史数据同理，不做回填。

## 5. 后端 API

### 5.1 端点

`GET /v1/storage`，挂在 `app/routers/documents.py` 的现有 router（`prefix="/v1"`）下，随 `documents.router` 自动注册，无需改 `app/main.py`。

### 5.2 鉴权与隔离

复用 `TenantId`（`app/deps.py`，来自 `X-Tenant-Id` 请求头，缺省 `demo`）。**不接受** query 里的 `tenant_id`——查询永远限定在当前请求头租户，保持与上传接口一致的多租户边界。

### 5.3 查询参数

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `kb_id` | str | 无 | 可选，按 KB 过滤；值需通过 `_validate_id` 白名单校验 |
| `status` | str | 无 | 可选，按状态过滤（`pending`/`ready`/`failed`），非法值忽略 |
| `limit` | int | 200 | 返回行数上限，范围 1–500 |

### 5.4 响应模型

在 `app/schemas.py` 新增：

```python
class StorageDocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    size: int | None
    status: str
    created_at: datetime

class StorageStats(BaseModel):
    total: int
    bytes: int
    by_status: dict[str, int]

class StorageResponse(BaseModel):
    backend: Literal["local", "s3"]
    bucket: str | None
    stats: StorageStats
    documents: list[StorageDocumentOut]
```

`backend` / `bucket` 由新增的 `describe_storage()`（见 §6）提供，不做任何存储 I/O（避免列表接口变慢或因后端不可用而 500）。

### 5.5 数据查询

一条带过滤的 SQL 取文档列表（按 `created_at DESC`），再在 Python 侧聚合计数与总字节：

```sql
SELECT id, kb_id, meta->>'filename' AS filename, (meta->>'size')::bigint AS size,
       status, created_at
FROM documents
WHERE tenant_id = :tenant
  [AND kb_id = :kb]
  [AND status = :status]
ORDER BY created_at DESC
LIMIT :limit
```

`stats.total` = 过滤后行数（注意：是当前过滤集的计数，不是全租户；语义在 UI 上要对应——状态卡展示「当前过滤结果」）。`stats.bytes` = 过滤集 `size` 求和（null 计 0）。`by_status` = 过滤集按状态分组计数。

> 说明：为保持实现简单，统计基于「当前过滤后的返回集」。如果后续需要「全租户总量不受过滤影响」，再拆分两次查询。首版不做。

### 5.6 错误处理

- `kb_id` 非法 → 400（复用 `_validate_id`）。
- 存储描述符读取失败（如未知 backend）→ 端点仍返回 200，`backend` 降级为 `"local"` 并在日志告警，不阻塞文档列表。

## 6. 存储描述符

在 `src/storage.py` 新增纯函数 `describe_storage() -> dict`：

```python
def describe_storage() -> dict:
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    bucket = os.environ.get("S3_BUCKET", "").strip() or None if backend == "s3" else None
    return {"backend": backend if backend in {"local", "s3"} else "local", "bucket": bucket}
```

只读环境变量，不实例化客户端、不做网络 I/O。未知 backend 降级 `"local"`（与 `build_storage` 的严格报错不同——这里是展示用，降级更稳；由调用方日志记录原值）。

## 7. 前端实现点

1. **`app/static/index.html`**：加导航项（§3.1）+ `view-storage` 容器。
2. **`app/static/js/app.mjs`**：
   - `import { createStorage } from "./views/storage.mjs";`
   - `VIEW_NAMES` 加 `"storage"`
   - 新增 `storageController` 变量；`showView` 里加 `storageController?.setActive(...)`；`clearViewState` 的硬编码视图列表加 `"storage"`、加 `storageController?.clear()`；`destroyWorkbench` 加 `destroy?.()`；`initializeWorkbench` 里创建 `createStorage({ root, guard, scope })`。
3. **`app/static/js/views/storage.mjs`**（新建）：照抄 `overview.mjs` 的 controller 骨架（`createStorage` 返回 `{ load, setActive, clear, destroy }`），用 `apiJson("/v1/storage", { scope, ... })` 拉数据并渲染；监听 `workbuddy:refresh` / `workbuddy:scopechange`。导出纯渲染函数便于单测（如 `formatBytes`、`statusLabel`）。
4. **i18n**：`en.mjs` / `zh-CN.mjs` 加 `nav.storage` 及 `storage.*` 文案（标题、各卡片、列名、状态标签、空态/错误态、只读提示）。
5. 复用 `core.mjs` 的 `escapeHtml`、`i18n` 的 `formatError`。

## 8. 测试策略

| 层级 | 内容 |
| --- | --- |
| 后端单测 | `tests/test_api_storage.py`：上传后 meta 写入 filename/size；`GET /v1/storage` 返回结构正确、kb/status 过滤生效、租户隔离（不同 X-Tenant-Id 互不可见）、非法 kb_id→400、local 后端 bucket 为 null。复用 `tests/conftest.py` 的客户端与 DB 夹具。 |
| 前端单测 | `tests/js/storage.test.mjs`：`formatBytes` 边界、状态标签映射、空态/错误态渲染；mock `apiJson` 验证请求带 scope header、过滤参数正确。照抄 `tests/js/overview.test.mjs` 的 FakeRoot/事件模式。 |
| 回归 | `py -3.13 -m pytest -q` 全绿；Node 显式文件列表全绿（含新增 `storage.test.mjs`）。 |
| 端到端（手动） | local 与 s3 各上传一份，视图显示文件名/大小/状态正确；切换租户看不到对方文档。 |

## 9. 验收标准

1. 左侧出现「存储」导航，点击进入独立视图，hash 路由 `#/storage` 生效。
2. 上传 `.docx` 后，meta 中含 `filename` 与 `size`；视图表格显示正确的文件名、大小、状态（pending→刷新后 ready）、时间。
3. 状态卡：后端、桶（local 显示「—」）、总数、总大小、就绪数与当前过滤结果一致（不加过滤时等于该租户全量）。
4. KB / 状态过滤生效；切换全局作用域的租户后，只看到该租户文档。
5. 非法 `kb_id` 返回 400；本视图无任何写操作（无上传/删除按钮）。
6. 中英文案完整，无裸 i18n key。
7. 全量 pytest + Node 测试绿。
