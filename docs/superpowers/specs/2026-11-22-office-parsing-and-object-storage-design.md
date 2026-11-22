# 办公文档解析与对象存储设计

- 日期：2026-11-22
- 状态：已确认，待实施
- 影响范围：入库管线（上传 → 解析 → 切块 → 向量化）、部署配置

## 1. 背景与目标

当前入库只接受 `.pdf` / `.md` / `.txt`。业务资料大量以 Word、PPT、Excel 形式存在，必须先手工转档才能入库；同时原始文件依赖本地 `data/uploads` 目录与 Docker 命名卷共享，API 与 worker 必须同机同卷部署。

目标：

1. 原生支持 `.docx`、`.pptx`、`.xlsx` 的上传与切片，检索体验与现有格式一致。
2. 引入对象存储（MinIO，S3 协议），本地用 Docker 起，后续可无缝切到云厂商对象存储。

## 2. 范围

### 做

- 三种格式解析为 Markdown 后走既有切块管线
- 存储抽象层：本地磁盘（默认）与 S3 兼容存储可切换
- MinIO 本地容器编排与初始化建桶
- 上传接口、worker 取文件路径改造
- 相应单元测试与端到端用例

### 不做

- `.doc` / `.ppt` / `.xls` 老格式（需 LibreOffice/COM，另行评估）
- 浏览器直传对象存储（预签名 URL）
- 保存解析中间产物、切片导出
- 文件下载 / 在线预览接口
- 对象存储的清理、生命周期、配额策略

## 3. 设计一：办公文档解析

### 3.1 总体思路

三种格式统一转换为 Markdown 文本，进入现有 `_extract_text` → `split_document` 流程。切块、父子块、Markdown 表格每 20 行拆分、overlap、向量化全部复用，不引入第二套管线。

### 3.2 各格式规则

**.docx**

- 按 `document.element.body` 子元素顺序遍历，保证段落与表格相对顺序不丢失
- 样式名匹配 `Heading 1/2/3` 与 `标题 1/2/3` → `#` / `##` / `###`
- 普通段落输出原文；空段落跳过
- 表格 → Markdown 表格（首行作表头）
- 跳过页眉、页脚、目录（TOC）段落

**.pptx**

- 按幻灯片顺序输出，每页起始 `## 第 N 页 · {标题}`；无标题时省略标题部分
- 文本框按段落输出原文
- 表格 → Markdown 表格
- 备注页 → `### 备注` 段落
- 组合形状递归展开

**.xlsx**

- 整本工作簿生成一个文档
- 每个 sheet → `## {sheet 名}`
- 首个非空行作为表头，无表头时生成 `列1..列N`
- `data_only=True` 读取公式缓存值；合并单元格取左上角值
- 跳过空 sheet
- 单 sheet 超过 **5000 行**时截断，并在该 sheet 末尾追加一行说明「已截断，仅索引前 5000 行」

### 3.3 边界与错误处理

| 场景 | 行为 |
| --- | --- |
| 文件加密 / 损坏 | 不可重试错误，任务直接失败，提示可读原因，不消耗 3 次重试 |
| 无任何可提取文本 | 失败，提示「文档无可提取文本」 |
| 单文件 > 20 MB | 上传阶段 422 |
| 图片、图表、形状内文字 | 不提取；纯图片 PPT 建议转 PDF 走 OCR |
| 后缀不在白名单 | 上传阶段 422 |

### 3.4 改动点

- 新增 `src/parsers/office.py`：`docx_to_markdown` / `pptx_to_markdown` / `xlsx_to_markdown`
- `src/ingest.py`：`_extract_text` 增加三个后缀分支，不可重试错误复用 `NonRetryableIngestError`
- `app/routers/documents.py`、`src/ingest_jobs.py`、`src/connectors/local_dir.py`：白名单增加 `.docx` `.pptx` `.xlsx`
- `app/static/index.html`：上传输入 `accept` 增加三种后缀
- `pyproject.toml`：新增 `python-docx`、`python-pptx`、`openpyxl`

## 4. 设计二：对象存储

### 4.1 存储抽象

新增 `src/storage.py`：

```python
class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...

def build_storage() -> Storage: ...
```

- `LocalStorage(root="data/uploads")`：默认，行为与现状一致
- `S3Storage`：boto3，`endpoint_url` 指向 MinIO，兼容 AWS S3 / 阿里 OSS / 腾讯 COS
- `build_storage()` 依据 `STORAGE_BACKEND` 选择

### 4.2 Key 布局

沿用现有相对路径，`documents.source_url` 语义不变：

```
{tenant_id}/{kb_id}/{doc_id}{suffix}
```

已有数据无需迁移；本地磁盘模式下 key 与文件路径一一对应。

### 4.3 配置

```
STORAGE_BACKEND=local|s3
S3_ENDPOINT=http://127.0.0.1:9000
S3_BUCKET=rag-kb
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_REGION=us-east-1
S3_USE_SSL=false
```

`STORAGE_BACKEND=local` 时不读取 S3 配置。

### 4.4 部署

- `deploy/docker-compose.yml` 增加 `minio` 服务（API 9000、控制台 9001、数据卷持久化）
- 增加一次性 `minio-init` 服务创建 bucket
- api 与 worker 注入 S3 环境变量
- 启用 s3 后不再依赖 `uploads` 命名卷；该卷保留，兼容 local 模式
- 生产环境把 `S3_ENDPOINT` 指向自建 MinIO 或云厂商地址即可

### 4.5 上传与读取链路

- 上传：API 接收 → 校验后缀与大小 → `storage.put_bytes(key, data)` → 落库入队
- 入库：worker 按 `source_url` 调用 `storage.get_bytes(key)` 解析，不再直接读文件系统
- 失败语义不变：文件缺失按不可重试错误处理

### 4.6 改动点

- 新增 `src/storage.py`
- `app/routers/documents.py`、`src/ingest_jobs.py`、`src/worker.py`：改为使用存储抽象
- `deploy/docker-compose.yml`、`.env.example`、`pyproject.toml`（boto3）

## 5. 测试策略

| 层级 | 内容 |
| --- | --- |
| 解析单测 | `tests/test_parsers_office.py`：最小 docx/pptx/xlsx 夹具，断言标题层级、表格转 Markdown、多 sheet、空文档、截断提示 |
| 上传接口 | 三种新后缀返回 202；不支持后缀 422；加密/损坏文件任务失败且错误可读 |
| 存储抽象 | `tests/test_storage.py`：本地实现的读写删；S3 实现用 mock/moto 覆盖调用与异常 |
| 端到端 | 上传 → worker 入库 → 检索命中，local 与 s3 两种后端各跑一次 |
| 回归 | 现有 Node 测试与 pytest UI 用例保持全绿 |

## 6. 发布与回滚

- 先发布解析扩展（无部署变化），单独验证
- 再发布存储抽象：默认仍 `local`，行为不变；切换 `STORAGE_BACKEND=s3` 需确认 bucket 已建、密钥可用
- 回滚方式：把 `STORAGE_BACKEND` 改回 `local` 即可，代码路径与 key 布局一致

## 7. 验收标准

1. 上传 `.docx` / `.pptx` / `.xlsx` 均返回 202，任务最终 `succeeded`
2. 三格式解析结果的 Markdown 标题层级与小节结构正确，表格可检索
3. `.xlsx` 多 sheet 全部进入同一文档，超 5000 行有截断说明
4. `STORAGE_BACKEND=s3` 时，上传文件出现在 MinIO，worker 能正常入库
5. `STORAGE_BACKEND=local` 下现有行为与测试完全不变
