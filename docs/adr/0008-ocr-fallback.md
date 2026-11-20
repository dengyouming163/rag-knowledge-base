# ADR 0008：parser 优先、本地 pypdf 降级，空块才启用 OCR

- **状态**：已采纳
- **日期**：2026-11-19

## 背景

文字型 PDF 需要保留正文顺序和表格结构，扫描件则没有可直接提取的文本层。OCR 对每页渲染和推理的成本明显高于普通文本提取，也可能改变正常文字型 PDF 的内容，因此不能对所有 PDF 无条件执行。与此同时，pdfplumber、图像渲染与 RapidOCR 依赖不应进入主 API/worker 镜像；parser 未部署或暂时不可用时，普通 PDF 仍应尽可能入库。

## 决策

- PDF 首选独立 parser 服务：pdfplumber 先提取文本与表格块，表格后续转换为 Markdown。只有这一步返回**空块列表**时才进入 `_ocr_pdf`；已有任意文本或表格块就不执行 OCR。
- OCR 在 parser 容器内把每页渲染为 200 DPI 图像并调用 RapidOCR。默认引擎只初始化一次，通过锁串行化初始化与 ONNX 推理；模型在 parser 镜像构建阶段预检，重依赖留在该镜像。
- 主服务只通过 `PdfParserClient` 调 parser。未配置 `PARSER_BASE_URL` 时直接使用本地 pypdf；parser 网络错误或 HTTP 非 2xx 时也退回本地 pypdf。本地路径只抽取文字层，**不会自行调用 RapidOCR**，也不保留 parser 的表格结构。
- parser 的 OCR 异常或 OCR 后仍无文本均返回可读的 422。客户端随后尝试本地 pypdf；若本地也无文本，则把“扫描件无文本 / 需要 OCR”归为 `NonRetryableIngestError`，worker 直接将任务置为 failed，不做无意义的三次重试。
- 若 parser 是网络故障或 5xx，而本地 pypdf 又无法提取文本，则保留可重试语义，由通用任务状态机最多尝试三次。不能因为本地无法 OCR，就把明确的远端服务故障永久判成文档错误。
- parser 在 Compose 中属于可选 `parser` profile。需要扫描件 OCR 时必须显式启动该服务；只有默认服务时，扫描件仍可能以“需要 OCR”不可重试失败。

## 后果

**好处**

- 正常文字型 PDF 不支付 OCR 的 CPU、内存和时延成本，也避免 OCR 改写本可直接提取的内容。
- parser 故障不阻断有可提取文字层的 PDF；主镜像保持轻量，本机开发与 CI 无需启动 parser 也能覆盖普通 PDF。
- OCR 的输出复用统一块结构和后续切分/入库链路；空结果与模型不可用都有可读错误，不会把空文档伪装成 ready。
- 对文档内容错误与远端暂时故障分别采用不可重试和可重试策略，减少无效重试，同时保留服务恢复后的入库机会。

**代价**

- 只有空块才触发 OCR 是有意的保守策略：只要文字层留下少量噪声块，就不会对整份 PDF 补做 OCR，可能漏掉其余扫描页。
- OCR 路径只产生逐页文本块，不恢复标题层级或表格结构；识别质量受扫描清晰度、版面和 200 DPI 渲染影响。
- 本地 pypdf 只是可用性降级，不等价于 parser：没有表格结构提取，也不能抢救扫描件。生产启用扫描件入库仍需部署 parser profile。
- RapidOCR 增大 parser 镜像与构建时间，每页图像渲染和 ONNX 推理增加 CPU / 内存占用；锁保证引擎复用与推理串行，也限制了单 parser 进程的 OCR 并发。
- parser 返回 4xx/5xx 后主服务还会再读一遍本地 PDF，失败请求会增加一次解析成本；需要根据任务错误区分文档问题与服务问题。

## 对应代码

- `parser/main.py`（pdfplumber 提取、空块判断、RapidOCR 与 422 响应）
- `parser/Dockerfile`（独立镜像、系统依赖与模型构建期预检）
- `parser/requirements.txt`（pdfplumber / RapidOCR 等 parser 专属依赖）
- `src/parsers/pdf.py`（parser HTTP 客户端、本地 pypdf 降级与错误分类）
- `src/ingest.py`（空文本检查与 NonRetryableIngestError 转换）
- `src/worker.py`（不可重试失败与通用重试分流）
- `deploy/docker-compose.yml`（可选 parser profile 与 worker 的 PARSER_BASE_URL）
- `tests/test_ocr_branch.py`（空块才 OCR、OCR 失败、引擎复用与逐页转换）
- `tests/test_ingest_jobs.py`（扫描件不可重试、远端故障可重试）
- `tests/test_ingest.py`（本地解析与入库边界）
