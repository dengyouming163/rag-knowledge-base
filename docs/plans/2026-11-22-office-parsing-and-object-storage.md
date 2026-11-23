# Office Parsing and Object Storage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 原生支持 `.docx` / `.pptx` / `.xlsx` 上传入库，并把原始文件读写从固定 `data/uploads` 目录抽象为「本地磁盘 / S3 兼容对象存储」可切换，默认仍为本地、行为不变。

**Architecture:** 三种办公格式在进程内解析为 Markdown，复用 `src/ingest.py::_extract_text` → `split_document` 既有管线，不新增第二套切块逻辑；原始文件读写收敛到 `src/storage.py` 的 `Storage` 协议，`build_storage()` 按 `STORAGE_BACKEND` 选择 `LocalStorage` 或 `S3Storage`，key 布局沿用 `{tenant_id}/{kb_id}/{doc_id}{suffix}`，`documents.source_url` 语义不变。设计与验收口径见 `docs/superpowers/specs/2026-11-22-office-parsing-and-object-storage-design.md`。

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2.0 / Postgres 16 + pgvector / python-docx / python-pptx / openpyxl / boto3 / MinIO / pytest / 原生 HTML/CSS/JS。

---

## 执行约定

- 所有命令在 `projects/rag-knowledge-base` 目录下执行；Python 一律 `py -3.13`（本机 `python` 是 2.7）。
- 每完成一个 Task：先写失败测试 → 运行确认 RED → 最小实现 → 运行确认 GREEN → 全量回归 → 只按文件名暂存并 commit。
- Python 测试：`py -3.13 -m pytest tests/test_xxx.py -q`；全量：`py -3.13 -m pytest -q`。
- Node 测试必须显式列出文件，不要用目录通配：`node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs`。
- 提交消息用仓库既有风格：`feat(scope): …` / `fix(scope): …` / `test(scope): …` / `chore(scope): …`；禁止 `git push`。
- 不提交 `.env`、`data/`、临时文件；不修改 `eval/baseline.json`、`eval/cases.jsonl`、`eval/demo_corpus.json`。
- 不做超出 spec 的能力：不支持 `.doc/.ppt/.xls`、不做浏览器直传（预签名 URL）、不保存解析中间产物、不提供下载/预览接口、不做对象存储生命周期管理。

---

## Phase 1 — Office 格式解析（.docx / .pptx / .xlsx）

### Task 1: 依赖声明与安装

**Files:**
- Modify: `pyproject.toml`（`[project].dependencies`，第 7–20 行）
- Modify: `Dockerfile`（builder 阶段依赖列表，第 20–32 行）
- Test: 无（依赖变更用导入命令验证）

**Step 1: 修改 `pyproject.toml`**

Before：

```toml
    "pypdf>=4.0",
    "numpy>=1.26",
```

After：

```toml
    "pypdf>=4.0",
    "python-docx>=1.1",
    "python-pptx>=1.0",
    "openpyxl>=3.1",
    "numpy>=1.26",
```

**Step 2: 同步修改 `Dockerfile`**

`Dockerfile` 的 builder 阶段硬编码依赖，不读 `pyproject.toml`；不同步会导致容器里解析报 `ModuleNotFoundError`。

Before：

```dockerfile
        "pypdf>=4.0" \
        "numpy>=1.26" \
```

After：

```dockerfile
        "pypdf>=4.0" \
        "python-docx>=1.1" \
        "python-pptx>=1.0" \
        "openpyxl>=3.1" \
        "numpy>=1.26" \
```

**Step 3: 安装到本机环境**

Run:

```powershell
py -3.13 -m pip install "python-docx>=1.1" "python-pptx>=1.0" "openpyxl>=3.1"
```

Expected: 末尾出现 `Successfully installed ... openpyxl-3.1.x python-docx-1.x python-pptx-1.x lxml-5.x ...`。

**Step 4: 验证可导入**

Run:

```powershell
py -3.13 -c "import importlib.metadata as m; print(m.version('python-docx'), m.version('python-pptx'), m.version('openpyxl'))"
```

Expected: 输出三个版本号，例如 `1.2.0 1.0.2 3.1.5`（版本号可不同，不能报 `PackageNotFoundError`）。

**Step 5: Commit**

```powershell
git add pyproject.toml Dockerfile
git commit -m "chore(deps): add office document libraries"
```

---

### Task 2: `.docx` 解析器

**Files:**
- Create: `src/parsers/office.py`
- Test: `tests/test_parsers_office.py`

**Step 1: 写失败测试**

`tests/test_parsers_office.py`（本 Task 只包含 docx 用例，后续 Task 追加）：

```python
"""办公文档解析单测：夹具用解析库现场生成，不提交二进制文件。"""

from __future__ import annotations

import io

import pytest
from docx import Document

from src.parsers.office import OfficeParseError, docx_to_markdown


def _docx_bytes() -> bytes:
    document = Document()
    document.add_heading("售后政策", level=1)
    document.add_paragraph("本政策适用于全部渠道。")
    document.add_heading("退款", level=2)
    document.add_heading("时限", level=3)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目"
    table.cell(0, 1).text = "说明"
    table.cell(1, 0).text = "退款"
    table.cell(1, 1).text = "七天无理由"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_headings_paragraphs_and_table() -> None:
    markdown = docx_to_markdown(_docx_bytes())

    assert markdown.startswith("# 售后政策")
    assert "## 退款" in markdown
    assert "### 时限" in markdown
    assert "| 项目 | 说明 |" in markdown
    assert "| --- | --- |" in markdown
    assert "| 退款 | 七天无理由 |" in markdown
    # 段落与表格必须保持文档中的先后顺序
    assert markdown.index("本政策适用于全部渠道。") < markdown.index("## 退款")
    assert markdown.index("## 退款") < markdown.index("| 项目 | 说明 |")


def test_docx_empty_document_returns_empty() -> None:
    buffer = io.BytesIO()
    Document().save(buffer)

    assert docx_to_markdown(buffer.getvalue()) == ""


def test_docx_corrupt_file_raises_readable_error() -> None:
    with pytest.raises(OfficeParseError) as exc_info:
        docx_to_markdown(b"not a docx")

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'src.parsers.office'`。

**Step 3: 创建 `src/parsers/office.py`（docx 部分）**

```python
"""办公文档解析：把 .docx / .pptx / .xlsx 转成 Markdown。

只提取文本与表格，不处理图片、图表和形状内文字。无法打开的文件统一抛
OfficeParseError，由 ingest._extract_text 转成不可重试的入库错误，避免
加密/损坏文件白白消耗三次重试。
"""

from __future__ import annotations

import io

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.tables import rows_to_markdown

_HEADING_STYLES = {
    "Heading 1": "#",
    "Heading 2": "##",
    "Heading 3": "###",
    "标题 1": "#",
    "标题 2": "##",
    "标题 3": "###",
}
_TOC_STYLE_PREFIXES = ("TOC", "toc", "目录")


class OfficeParseError(RuntimeError):
    """文件已加密、损坏或结构不可识别；重试不会改变结果。"""


def _iter_docx_blocks(document):
    """按 body 子元素顺序产出段落与表格，避免表格位置被挪到文末。"""
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def _is_toc_paragraph(paragraph: Paragraph) -> bool:
    style = paragraph.style.name if paragraph.style is not None else ""
    return style.startswith(_TOC_STYLE_PREFIXES)


def docx_to_markdown(content: bytes) -> str:
    """docx → Markdown；页眉/页脚不在 body 中，天然被跳过。"""
    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - 加密/损坏统一给可读提示
        raise OfficeParseError("无法打开 .docx 文件（可能已加密或损坏）") from exc

    blocks: list[str] = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, Table):
            rows = [[cell.text.strip() for cell in row.cells] for row in block.rows]
            table = rows_to_markdown(rows)
            if table:
                blocks.append(table)
            continue

        if _is_toc_paragraph(block):
            continue
        text = block.text.strip()
        if not text:
            continue
        style_name = block.style.name if block.style is not None else ""
        prefix = _HEADING_STYLES.get(style_name)
        blocks.append(f"{prefix} {text}" if prefix else text)

    return "\n\n".join(blocks).strip()
```

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: `3 passed`。

**Step 5: Commit**

```powershell
git add src/parsers/office.py tests/test_parsers_office.py
git commit -m "feat(ingest): parse docx to markdown"
```

---

### Task 3: `.pptx` 解析器

**Files:**
- Modify: `src/parsers/office.py`
- Modify: `tests/test_parsers_office.py`

**Step 1: 追加失败测试**

在 `tests/test_parsers_office.py` 头部导入区补：

Before：

```python
import pytest
from docx import Document

from src.parsers.office import OfficeParseError, docx_to_markdown
```

After：

```python
import pytest
from docx import Document
from pptx import Presentation
from pptx.util import Inches

from src.parsers.office import (
    OfficeParseError,
    docx_to_markdown,
    pptx_to_markdown,
)
```

在文件末尾追加：

```python
def _pptx_bytes() -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "季度目标"

    body = slide.placeholders[1].text_frame
    body.text = "营收增长 20%"
    body.add_paragraph().text = "费用下降 5%"

    table = slide.shapes.add_table(
        2, 2, Inches(1), Inches(4), Inches(4), Inches(1)
    ).table
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "目标"
    table.cell(1, 0).text = "营收"
    table.cell(1, 1).text = "20%"

    slide.notes_slide.notes_text_frame.text = "备注：口径为同比。"

    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def test_pptx_slide_title_text_table_and_notes() -> None:
    markdown = pptx_to_markdown(_pptx_bytes())

    assert "## 第 1 页 · 季度目标" in markdown
    assert markdown.count("季度目标") == 1  # 标题占位符不能被正文重复输出
    assert "营收增长 20%" in markdown
    assert "费用下降 5%" in markdown
    assert "| 指标 | 目标 |" in markdown
    assert "| 营收 | 20% |" in markdown
    assert "### 备注" in markdown
    assert "备注：口径为同比。" in markdown


def test_pptx_empty_presentation_returns_empty() -> None:
    presentation = Presentation()
    buffer = io.BytesIO()
    presentation.save(buffer)

    assert pptx_to_markdown(buffer.getvalue()) == ""


def test_pptx_corrupt_file_raises_readable_error() -> None:
    with pytest.raises(OfficeParseError) as exc_info:
        pptx_to_markdown(b"not a pptx")

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: FAIL，`ImportError: cannot import name 'pptx_to_markdown'`。

**Step 3: 实现 pptx 解析**

在 `src/parsers/office.py` 导入区补：

Before：

```python
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.tables import rows_to_markdown
```

After：

```python
from docx.table import Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from src.tables import rows_to_markdown
```

在 `docx_to_markdown` 之后追加：

```python
def _pptx_table_markdown(table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    return rows_to_markdown(rows)


def _pptx_shape_blocks(shape) -> list[str]:
    """组合形状递归展开；表格优先于文本框判断。"""
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        blocks: list[str] = []
        for child in shape.shapes:
            blocks.extend(_pptx_shape_blocks(child))
        return blocks

    if getattr(shape, "has_table", False):
        table = _pptx_table_markdown(shape.table)
        return [table] if table else []

    if getattr(shape, "has_text_frame", False):
        return [
            text
            for text in (paragraph.text.strip() for paragraph in shape.text_frame.paragraphs)
            if text
        ]
    return []


def pptx_to_markdown(content: bytes) -> str:
    """pptx → Markdown；每页一个 `## 第 N 页 · 标题`，备注在 `### 备注` 下。"""
    try:
        presentation = Presentation(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise OfficeParseError("无法打开 .pptx 文件（可能已加密或损坏）") from exc

    blocks: list[str] = []
    for index, slide in enumerate(presentation.slides, start=1):
        title_shape = slide.shapes.title
        title = title_shape.text.strip() if title_shape is not None else ""
        blocks.append(f"## 第 {index} 页 · {title}" if title else f"## 第 {index} 页")

        for shape in slide.shapes:
            if title_shape is not None and shape.shape_id == title_shape.shape_id:
                continue
            blocks.extend(_pptx_shape_blocks(shape))

        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append("### 备注")
                blocks.append(notes)

    return "\n\n".join(blocks).strip()
```

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: `6 passed`。

**Step 5: Commit**

```powershell
git add src/parsers/office.py tests/test_parsers_office.py
git commit -m "feat(ingest): parse pptx to markdown"
```

---

### Task 4: `.xlsx` 解析器

**Files:**
- Modify: `src/parsers/office.py`
- Modify: `tests/test_parsers_office.py`

**Step 1: 追加失败测试**

导入区追加：

```python
from openpyxl import Workbook

from src.parsers.office import (
    MAX_SHEET_ROWS,
    TRUNCATION_NOTE,
    OfficeParseError,
    docx_to_markdown,
    pptx_to_markdown,
    xlsx_to_markdown,
)
```

（替换 Task 3 的 `from src.parsers.office import (...)` 块。）

文件末尾追加：

```python
def _save_workbook(workbook: Workbook) -> bytes:
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _multi_sheet_bytes() -> bytes:
    workbook = Workbook()
    policy = workbook.active
    policy.title = "政策"
    policy.append(["编号", "标题"])
    policy.append([1, "退款"])

    empty = workbook.create_sheet("空表")
    empty.append([None, None])

    price = workbook.create_sheet("价格")
    price.append(["SKU", "价格"])
    price.append(["A-1", 99])
    return _save_workbook(workbook)


def test_xlsx_multiple_sheets_and_empty_sheet_skipped() -> None:
    markdown = xlsx_to_markdown(_multi_sheet_bytes())

    assert "## 政策" in markdown
    assert "## 价格" in markdown
    assert "## 空表" not in markdown
    assert "| 编号 | 标题 |" in markdown
    assert "| SKU | 价格 |" in markdown
    assert markdown.index("## 政策") < markdown.index("## 价格")


def test_xlsx_synthesizes_columns_when_first_row_is_data() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet.append([1, 2])
    sheet.append([3, 4])

    markdown = xlsx_to_markdown(_save_workbook(workbook))

    assert "| 列1 | 列2 |" in markdown
    assert "| 1 | 2 |" in markdown


def test_xlsx_merged_cells_use_top_left_value() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "合并"
    sheet["A1"] = "地区"
    sheet["C1"] = "金额"
    sheet.merge_cells("A1:B1")
    sheet["A2"] = "华东"
    sheet["C2"] = 100

    markdown = xlsx_to_markdown(_save_workbook(workbook))

    assert "| 地区 | 地区 | 金额 |" in markdown


def test_xlsx_truncates_after_5000_rows() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "长表"
    sheet.append(["编号", "标题"])
    for index in range(1, MAX_SHEET_ROWS + 1):
        sheet.append([index, f"第 {index} 行"])

    markdown = xlsx_to_markdown(_save_workbook(workbook))

    assert TRUNCATION_NOTE in markdown


def test_xlsx_corrupt_file_raises_readable_error() -> None:
    with pytest.raises(OfficeParseError) as exc_info:
        xlsx_to_markdown(b"not an xlsx")

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: FAIL，`ImportError: cannot import name 'MAX_SHEET_ROWS'`。

**Step 3: 实现 xlsx 解析**

导入区补：

```python
from typing import Any

from openpyxl import load_workbook
```

（放在 `import io` 之后，与 `from docx...` 之间空一行。）

模块顶部常量（`_HEADING_STYLES` 之前）：

```python
MAX_SHEET_ROWS = 5000
TRUNCATION_NOTE = "已截断，仅索引前 5000 行"
```

在 `pptx_to_markdown` 之后追加：

```python
def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _merged_cell_values(sheet) -> dict[tuple[int, int], Any]:
    """合并区域的所有坐标都取左上角值，避免右/下侧出现空洞。"""
    values: dict[tuple[int, int], Any] = {}
    for merged in sheet.merged_cells.ranges:
        value = sheet.cell(row=merged.min_row, column=merged.min_col).value
        for row in range(merged.min_row, merged.max_row + 1):
            for column in range(merged.min_col, merged.max_col + 1):
                values[(row, column)] = value
    return values


def _is_number(text: str) -> bool:
    if not text:
        return True
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def _sheet_table(sheet) -> tuple[list[list[str]], bool]:
    """返回 (含表头的二维表, 是否截断)；空 sheet 返回 ([], False)。

    只保留前 MAX_SHEET_ROWS 行（含表头），第 MAX_SHEET_ROWS + 1 行存在
    非空内容时标记截断，避免格式化的空白行造成误报。
    """
    merged = _merged_cell_values(sheet)
    limit = min(sheet.max_row or 0, MAX_SHEET_ROWS + 1)

    raw: list[list[str]] = []
    truncated = False
    for row_index, row in enumerate(
        sheet.iter_rows(min_row=1, max_row=limit, values_only=True), start=1
    ):
        values = [
            _cell_text(value if value is not None else merged.get((row_index, column)))
            for column, value in enumerate(row, start=1)
        ]
        if row_index > MAX_SHEET_ROWS:
            if any(values):
                truncated = True
            break
        raw.append(values)

    while raw and not any(raw[-1]):
        raw.pop()
    while raw and not any(raw[0]):
        raw.pop(0)
    if not raw:
        return [], truncated

    width = max(len(row) for row in raw)
    rows = [row + [""] * (width - len(row)) for row in raw]
    first = rows[0]
    if any(cell for cell in first if not _is_number(cell)):
        header, body = first, rows[1:]
    else:
        header = [f"列{index}" for index in range(1, width + 1)]
        body = rows
    return [header, *body], truncated


def xlsx_to_markdown(content: bytes) -> str:
    """整本工作簿 → 单个 Markdown 文档，每个非空 sheet 一个 `## {sheet 名}`。"""
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise OfficeParseError("无法打开 .xlsx 文件（可能已加密或损坏）") from exc

    blocks: list[str] = []
    try:
        for sheet in workbook.worksheets:
            rows, truncated = _sheet_table(sheet)
            if not rows:
                continue
            blocks.append(f"## {sheet.title}")
            blocks.append(rows_to_markdown(rows))
            if truncated:
                blocks.append(TRUNCATION_NOTE)
    finally:
        workbook.close()

    return "\n\n".join(blocks).strip()
```

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: `11 passed`。

**Step 5: Commit**

```powershell
git add src/parsers/office.py tests/test_parsers_office.py
git commit -m "feat(ingest): parse xlsx to markdown"
```

---

### Task 5: 接入 `_extract_text` 与入库错误语义

**Files:**
- Modify: `src/ingest.py`（导入区第 26–28 行、`_TEXT_SUFFIXES` 第 30 行、`_extract_text` 第 39–66 行）
- Modify: `tests/test_ingest.py`（文件末尾追加）

**Step 1: 写失败测试**

在 `tests/test_ingest.py` 导入区补：

```python
from docx import Document
```

在文件末尾追加：

```python
def _write_docx(path) -> None:
    document = Document()
    document.add_heading("售后政策", level=1)
    document.add_heading("退款", level=2)
    document.add_paragraph("支持七天无理由退款。")
    document.save(path)


def test_ingest_docx_end_to_end(engine, tmp_path) -> None:
    path = tmp_path / "policy.docx"
    _write_docx(path)

    result = ingest_document(
        engine,
        tenant_id=TENANT,
        kb_id=KB,
        doc_id=DOC,
        path=path,
        embed_client=FakeEmbed(),
    )

    assert result["chunks"] >= 1
    assert result["doc_status"] == "ready"
    assert _doc_status(engine) == "ready"


def test_corrupt_docx_raises_non_retryable_ingest_error(tmp_path) -> None:
    path = tmp_path / "corrupt.docx"
    path.write_bytes(b"not a docx")

    with pytest.raises(NonRetryableIngestError) as exc_info:
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
        )

    assert "加密" in str(exc_info.value)
    assert "损坏" in str(exc_info.value)


def test_empty_docx_raises_non_retryable_with_no_text_message(tmp_path) -> None:
    path = tmp_path / "empty.docx"
    Document().save(path)

    with pytest.raises(NonRetryableIngestError) as exc_info:
        ingest_document(
            None,  # type: ignore[arg-type]
            tenant_id=TENANT,
            kb_id=KB,
            doc_id=DOC,
            path=path,
            embed_client=FakeEmbed(),
        )

    assert "无可提取文本" in str(exc_info.value)
```

同时把既有「不支持后缀」用例改成真正的旧格式 `.doc`（`.docx` 即将进入白名单）：

Before（第 494–497 行）：

```python
def test_unsupported_extension_raises_before_touching_database(tmp_path) -> None:
    """engine 传 None：后缀校验必须在建 store / 连库之前失败。"""
    path = tmp_path / "policy.docx"
    path.write_bytes(b"whatever")
```

After：

```python
def test_unsupported_extension_raises_before_touching_database(tmp_path) -> None:
    """engine 传 None：后缀校验必须在建 store / 连库之前失败。"""
    path = tmp_path / "policy.doc"
    path.write_bytes(b"whatever")
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_ingest.py -q
```

Expected: FAIL，`ValueError: 不支持的文件类型：.docx`（三个新用例失败）。

**Step 3: 修改 `src/ingest.py`**

导入区 Before：

```python
from .bm25_store import BM25Store
from .chunk import split_document
from .parsers.pdf import ParserUnavailable, PdfParserClient, blocks_to_text
from .store_pg import PgVectorStore

_TEXT_SUFFIXES = {".md", ".txt"}
```

After：

```python
from .bm25_store import BM25Store
from .chunk import split_document
from .parsers.office import (
    OfficeParseError,
    docx_to_markdown,
    pptx_to_markdown,
    xlsx_to_markdown,
)
from .parsers.pdf import ParserUnavailable, PdfParserClient, blocks_to_text
from .store_pg import PgVectorStore

_TEXT_SUFFIXES = {".md", ".txt"}
_OFFICE_PARSERS = {
    ".docx": docx_to_markdown,
    ".pptx": pptx_to_markdown,
    ".xlsx": xlsx_to_markdown,
}
```

`_extract_text` Before（第 39–66 行）：

```python
def _extract_text(file_path: Path, parser: Any) -> str:
    """按后缀提取纯文本/Markdown；不支持的格式直接抛 ValueError。"""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        content = file_path.read_bytes()
        ...
    if suffix in _TEXT_SUFFIXES:
        return file_path.read_text(encoding="utf-8")

    raise ValueError(f"不支持的文件类型：{suffix or '(none)'}")
```

After（只展示插入位置，PDF 分支内部保持不变）：

```python
def _extract_text(file_path: Path, parser: Any) -> str:
    """按后缀提取纯文本/Markdown；不支持的格式直接抛 ValueError。"""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        content = file_path.read_bytes()
        ...  # 保持原 PDF 分支不变

    if suffix in _OFFICE_PARSERS:
        try:
            extracted = _OFFICE_PARSERS[suffix](file_path.read_bytes())
        except OfficeParseError as exc:
            raise NonRetryableIngestError(str(exc)) from exc
        if not extracted.strip():
            # 空文档继续走流程会被标成 ready 却没有任何块，直接判不可重试失败
            raise NonRetryableIngestError(
                f"{suffix} 文档无可提取文本"
                "（图片、图表和形状内文字不提取，可转 PDF 后走 OCR）"
            )
        return extracted

    if suffix in _TEXT_SUFFIXES:
        return file_path.read_text(encoding="utf-8")

    raise ValueError(f"不支持的文件类型：{suffix or '(none)'}")
```

**Step 4: 跑测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_ingest.py -q
```

Expected: 全部 passed（数据库不可用时集成用例自动 skip，三个新用例仍应通过两个纯解析用例）。

**Step 5: Commit**

```powershell
git add src/ingest.py tests/test_ingest.py
git commit -m "feat(ingest): support office formats in ingest pipeline"
```

---

### Task 6: 白名单、前端上传入口与接口测试

**Files:**
- Modify: `app/routers/documents.py`（第 35 行）
- Modify: `src/ingest_jobs.py`（第 29 行）
- Modify: `src/connectors/local_dir.py`（第 15 行）
- Modify: `app/static/index.html`（第 255 行）
- Modify: `app/static/js/views/lab.mjs`（第 643 行）
- Modify: `app/static/js/i18n/en.mjs`（第 140 行）
- Modify: `app/static/js/i18n/zh-CN.mjs`（第 140 行）
- Modify: `tests/test_api_documents.py`（第 115–118 行、文件末尾）
- Modify: `tests/test_connector_local.py`（第 26 行）
- Modify: `tests/test_worker_connectors.py`（第 361 行）
- Modify: `tests/js/lab.test.mjs`（第 316 行）

**Step 1: 写失败测试**

`tests/test_api_documents.py`：把「拒绝非 PDF」改成「拒绝不支持的旧格式」，否则 `.docx` 现在应被接受。

Before（第 115–118 行）：

```python
def test_upload_rejects_non_pdf(client: TestClient) -> None:
    response = _upload(client, b"not a pdf", filename="policy.docx")

    assert response.status_code == 422
```

After：

```python
def test_upload_rejects_unsupported_suffix(client: TestClient) -> None:
    response = _upload(client, b"legacy office", filename="policy.doc")

    assert response.status_code == 422
```

文件末尾追加：

```python
@pytest.mark.parametrize("filename", ["policy.docx", "policy.pptx", "policy.xlsx"])
def test_upload_accepts_office_formats(client: TestClient, filename: str) -> None:
    response = _upload(client, f"office-{filename}".encode(), filename=filename)

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]
    suffix = Path(filename).suffix
    assert (UPLOAD_ROOT / TENANT / KB / f"{doc_id}{suffix}").is_file()
```

白名单扩展后，两处把 `.docx` 当作「不支持」的既有夹具会失效，一并改成 `.doc`：

`tests/test_connector_local.py` 第 26 行：

Before：

```python
    (tmp_path / "ignored.docx").write_bytes(b"ignored")
```

After：

```python
    (tmp_path / "ignored.doc").write_bytes(b"ignored")
```

`tests/test_worker_connectors.py` 第 361 行：

Before：

```python
        (TENANT, KB, "file:///source/a.docx", b"content"),
```

After：

```python
        (TENANT, KB, "file:///source/a.doc", b"content"),
```

**Step 2: 跑测试确认失败**

Run:

```powershell
py -3.13 -m pytest tests/test_api_documents.py -q
```

Expected: FAIL，三个新用例返回 422（`unsupported file type: .docx`）。

**Step 3: 更新后端白名单**

`app/routers/documents.py`：

Before：`ALLOWED_SUFFIX = {".pdf", ".md", ".txt"}`
After：`ALLOWED_SUFFIX = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}`

`src/ingest_jobs.py`：

Before：`ALLOWED_SOURCE_SUFFIXES = {".pdf", ".md", ".txt"}`
After：`ALLOWED_SOURCE_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}`

`src/connectors/local_dir.py`：

Before：`_SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt"}`
After：`_SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}`

**Step 4: 跑接口测试确认通过**

Run:

```powershell
py -3.13 -m pytest tests/test_api_documents.py -q
```

Expected: `11 passed`（本机数据库可用时；数据库不可用时相关用例自动 skip）。

**Step 5: 同步前端上传白名单**

只改 `accept` 属性而不改 JS 正则，浏览器仍会拦截办公文件，所以三处一起改。

`app/static/index.html` 第 255 行：

Before：

```html
<input id="upload-input" name="file" type="file" accept=".pdf,.md,.txt" required>
```

After：

```html
<input id="upload-input" name="file" type="file" accept=".pdf,.md,.txt,.docx,.pptx,.xlsx" required>
```

`app/static/js/views/lab.mjs` 第 643 行：

Before：

```javascript
      if (!/\.(pdf|md|txt)$/i.test(file.name)) throw new Error(t("lab.validation.fileType"));
```

After：

```javascript
      if (!/\.(pdf|md|txt|docx|pptx|xlsx)$/i.test(file.name)) throw new Error(t("lab.validation.fileType"));
```

`app/static/js/i18n/en.mjs` 第 140 行：

Before：

```javascript
  "lab.validation.fileType": "Only .pdf, .md, and .txt files are supported.",
```

After：

```javascript
  "lab.validation.fileType": "Only .pdf, .md, .txt, .docx, .pptx, and .xlsx files are supported.",
```

`app/static/js/i18n/zh-CN.mjs` 第 140 行：

Before：

```javascript
  "lab.validation.fileType": "仅支持 .pdf、.md 和 .txt 文件。",
```

After：

```javascript
  "lab.validation.fileType": "仅支持 .pdf、.md、.txt、.docx、.pptx 和 .xlsx 文件。",
```

`tests/js/lab.test.mjs` 第 316 行：

Before：

```javascript
  assert.equal(root.text("#upload-status"), "Only .pdf, .md, and .txt files are supported.");
```

After：

```javascript
  assert.equal(
    root.text("#upload-status"),
    "Only .pdf, .md, .txt, .docx, .pptx, and .xlsx files are supported.",
  );
```

**Step 6: 跑 Node 测试确认通过**

Run:

```powershell
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs
```

Expected: 末尾 `# fail 0`。

**Step 7: Commit**

```powershell
git add app/routers/documents.py src/ingest_jobs.py src/connectors/local_dir.py app/static/index.html app/static/js/views/lab.mjs app/static/js/i18n/en.mjs app/static/js/i18n/zh-CN.mjs tests/test_api_documents.py tests/test_connector_local.py tests/test_worker_connectors.py tests/js/lab.test.mjs
git commit -m "feat(upload): accept docx, pptx and xlsx uploads"
```

---

### Task 7: Phase 1 全量回归

**Files:** 无新增/修改，只验证。

**Step 1: 跑解析与入库相关测试**

```powershell
py -3.13 -m pytest tests/test_parsers_office.py tests/test_ingest.py tests/test_api_documents.py tests/test_ingest_jobs.py tests/test_connector_local.py -q
```

Expected: 全部 passed（数据库不可用时相关用例 skip），不得出现 failed。

**Step 2: 跑全量 Python 测试**

```powershell
py -3.13 -m pytest -q
```

Expected: 末尾无 `failed`；既有用例保持全绿。

**Step 3: 跑 Node 测试**

```powershell
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs
```

Expected: `# fail 0`。

**Step 4: 抽查真实上传（可选，需本地库与服务）**

```powershell
py -3.13 -m uvicorn app.main:app --port 8000
```

另开终端：

```powershell
curl.exe -s -o NUL -w "%{http_code}" -H "X-Tenant-Id: demo" -F "file=@docs/plans/2026-11-22-office-parsing-and-object-storage.md;filename=plan.docx" http://127.0.0.1:8000/v1/kbs/demo/documents
```

Expected: `202`（文件内容不重要，接口只校验后缀；若 demo 库不存在返回 404 则先建库）。

---

## Phase 2 — 存储抽象 + MinIO

### Task 1: 依赖声明与安装（boto3）

**Files:**
- Modify: `pyproject.toml`
- Modify: `Dockerfile`

**Step 1: 修改 `pyproject.toml`**

Before：

```toml
    "pgvector>=0.3",
]
```

After：

```toml
    "pgvector>=0.3",
    "boto3>=1.34",
]
```

**Step 2: 同步修改 `Dockerfile`**

Before：

```dockerfile
        "psycopg[binary]>=3.1" \
        "pgvector>=0.3"
```

After：

```dockerfile
        "psycopg[binary]>=3.1" \
        "pgvector>=0.3" \
        "boto3>=1.34"
```

**Step 3: 安装并验证**

```powershell
py -3.13 -m pip install "boto3>=1.34"
py -3.13 -c "import importlib.metadata as m; print(m.version('boto3'), m.version('botocore'))"
```

Expected: 输出两个版本号，不报错。

**Step 4: Commit**

```powershell
git add pyproject.toml Dockerfile
git commit -m "chore(deps): add boto3 for object storage"
```

---

### Task 2: `src/storage.py` 存储抽象

**Files:**
- Create: `src/storage.py`
- Test: `tests/test_storage.py`

**Step 1: 写失败测试**

`tests/test_storage.py`：

```python
"""存储抽象单测：本地读写删、S3 请求参数与错误传播、后端选择。"""

from __future__ import annotations

import io

import boto3
import pytest
from botocore.stub import Stubber

from src.storage import LocalStorage, S3Storage, StorageError, build_storage


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:9000",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def test_local_roundtrip_and_delete(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    storage.put_bytes("t/kb/doc1.docx", b"payload")

    assert storage.exists("t/kb/doc1.docx") is True
    assert storage.get_bytes("t/kb/doc1.docx") == b"payload"
    assert (tmp_path / "t" / "kb" / "doc1.docx").read_bytes() == b"payload"

    storage.delete("t/kb/doc1.docx")

    assert storage.exists("t/kb/doc1.docx") is False
    with pytest.raises(FileNotFoundError):
        storage.get_bytes("t/kb/doc1.docx")


def test_local_rejects_path_traversal(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    with pytest.raises(StorageError, match="非法存储 key"):
        storage.put_bytes("../escape.txt", b"x")


def test_local_write_error_propagates(tmp_path) -> None:
    root = tmp_path / "root"
    root.write_text("not a directory", encoding="utf-8")
    storage = LocalStorage(root)

    with pytest.raises(OSError):
        storage.put_bytes("t/kb/doc1.md", b"x")


def test_s3_roundtrip_uses_bucket_and_key() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx", "Body": b"payload"},
        )
        stubber.add_response(
            "head_object", {}, {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"}
        )
        stubber.add_response(
            "get_object",
            {"Body": io.BytesIO(b"payload")},
            {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"},
        )
        stubber.add_response(
            "delete_object", {}, {"Bucket": "rag-kb", "Key": "t/kb/doc1.docx"}
        )

        storage.put_bytes("t/kb/doc1.docx", b"payload")
        assert storage.exists("t/kb/doc1.docx") is True
        assert storage.get_bytes("t/kb/doc1.docx") == b"payload"
        storage.delete("t/kb/doc1.docx")


def test_s3_exists_returns_false_for_missing_key() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "head_object", service_error_code="404", http_status_code=404
        )

        assert storage.exists("t/kb/missing.md") is False


def test_s3_get_missing_key_raises_file_not_found() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_object", service_error_code="NoSuchKey", http_status_code=404
        )

        with pytest.raises(FileNotFoundError):
            storage.get_bytes("t/kb/missing.md")


def test_s3_put_failure_becomes_storage_error() -> None:
    client = _s3_client()
    storage = S3Storage(client=client, bucket="rag-kb")

    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="InternalError", http_status_code=500
        )

        with pytest.raises(StorageError, match="S3 写入失败"):
            storage.put_bytes("t/kb/doc1.md", b"x")


def test_build_storage_defaults_to_local(monkeypatch) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    storage = build_storage()

    assert isinstance(storage, LocalStorage)


def test_build_storage_selects_s3(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("S3_BUCKET", "rag-kb")
    monkeypatch.setenv("S3_ACCESS_KEY", "test")
    monkeypatch.setenv("S3_SECRET_KEY", "test")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_USE_SSL", "false")

    storage = build_storage()

    assert isinstance(storage, S3Storage)
    assert storage.bucket == "rag-kb"


def test_build_storage_rejects_unknown_backend(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "ftp")

    with pytest.raises(ValueError, match="STORAGE_BACKEND"):
        build_storage()
```

**Step 2: 跑测试确认失败**

```powershell
py -3.13 -m pytest tests/test_storage.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'src.storage'`。

**Step 3: 创建 `src/storage.py`**

```python
"""原始文件存储抽象：本地磁盘（默认）与 S3 兼容对象存储。

key 布局沿用现有相对路径 `{tenant_id}/{kb_id}/{doc_id}{suffix}`，
因此 `documents.source_url` 语义不变，已有本地数据无需迁移。
`STORAGE_BACKEND=local` 时不读取任何 S3 配置。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_UPLOAD_ROOT = Path("data/uploads")


class StorageError(RuntimeError):
    """存储后端不可用或返回错误；调用方按可重试失败处理。"""


class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    """本地磁盘实现；写入先落临时文件再 replace，保留原有的原子性。"""

    def __init__(self, root: str | Path = DEFAULT_UPLOAD_ROOT) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        root = self.root.resolve()
        path = (self.root / key).resolve()
        if path != root and root not in path.parents:
            raise StorageError(f"非法存储 key：{key}")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    """boto3 实现；MinIO 必须用 path-style 寻址。"""

    def __init__(
        self,
        *,
        bucket: str,
        client: Any = None,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        use_ssl: bool = False,
    ) -> None:
        self.bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region,
            use_ssl=use_ssl,
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"}
            ),
        )

    def put_bytes(self, key: str, data: bytes) -> None:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"S3 写入失败：{key}：{exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise StorageError(f"S3 读取失败：{key}：{exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 读取失败：{key}：{exc}") from exc
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                return False
            raise StorageError(f"S3 探测失败：{key}：{exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 探测失败：{key}：{exc}") from exc
        return True

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (ClientError, BotoCoreError) as exc:
            raise StorageError(f"S3 删除失败：{key}：{exc}") from exc


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"STORAGE_BACKEND=s3 时必须配置 {name}")
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def build_storage() -> Storage:
    """按 `STORAGE_BACKEND` 选择实现；未知值直接报错，避免静默降级。"""
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"
    if backend == "local":
        return LocalStorage(DEFAULT_UPLOAD_ROOT)
    if backend == "s3":
        return S3Storage(
            endpoint_url=os.environ.get("S3_ENDPOINT", ""),
            bucket=_required_env("S3_BUCKET"),
            access_key=os.environ.get("S3_ACCESS_KEY", ""),
            secret_key=os.environ.get("S3_SECRET_KEY", ""),
            region=os.environ.get("S3_REGION", "us-east-1"),
            use_ssl=_env_bool("S3_USE_SSL", False),
        )
    raise ValueError(
        f"未知的 STORAGE_BACKEND：{backend}（可选 local / s3）"
    )
```

**Step 4: 跑测试确认通过**

```powershell
py -3.13 -m pytest tests/test_storage.py -q
```

Expected: `10 passed`。

**Step 5: Commit**

```powershell
git add src/storage.py tests/test_storage.py
git commit -m "feat(storage): add local and s3 storage backends"
```

---

### Task 3: 上传、收件、worker 改用存储抽象

**Files:**
- Modify: `src/ingest.py`（`_extract_text` 签名、`ingest_document` 签名）
- Modify: `src/worker.py`（导入区第 7–27 行、取文件第 74–102 行、`worker_once` 签名第 30–36 行）
- Modify: `app/routers/documents.py`（导入区、第 36 行、第 134–149 行）
- Modify: `src/ingest_jobs.py`（导入区、第 30 行、`receive_source_document` 签名与第 148–159 行）
- Modify: `tests/test_ingest_jobs.py`（第 268 / 305 / 332 / 361 / 415 行、第 299–319 行）
- Modify: `tests/test_worker_connectors.py`（第 192 行）
- Create: `tests/test_storage_e2e.py`

**Step 1: 写失败的端到端测试**

`tests/test_storage_e2e.py`：

```python
"""存储抽象端到端：上传 → worker 入库 → 检索命中（local 后端）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import create_app
from src.bm25_store import BM25Store
from src.db.base import create_db_engine
from src.retrieve import HybridRetriever
from src.storage import LocalStorage
from src.store_pg import PgVectorStore
from src.worker import worker_once

TENANT = "t_storage_e2e"
KB = "kb_storage_e2e"
DIM = 1024

CONTENT = "# 售后政策\n\n## 退款\n\n支持七天无理由退款。".encode()


class FakeEmbed:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[float(index + 1)] * DIM for index, _ in enumerate(texts)]


def _cleanup(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM ingest_jobs WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM kb_bm25 WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM chunks WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM documents WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(
            text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": TENANT}
        )
        connection.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


@pytest.fixture()
def engine():
    db_engine = create_db_engine()
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"数据库不可用：{exc}")

    _cleanup(db_engine)
    with db_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name) VALUES (:t, 'storage')"),
            {"t": TENANT},
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bases (id, tenant_id, name, description, status) "
                "VALUES (:kb, :t, 'storage kb', '', 'active')"
            ),
            {"kb": KB, "t": TENANT},
        )
    yield db_engine
    _cleanup(db_engine)
    db_engine.dispose()


def test_upload_worker_and_search_use_local_storage(engine, tmp_path, monkeypatch) -> None:
    storage = LocalStorage(tmp_path)
    monkeypatch.setattr("app.routers.documents.build_storage", lambda: storage)

    client = TestClient(create_app())
    response = client.post(
        f"/v1/kbs/{KB}/documents",
        files={"file": ("policy.md", CONTENT, "text/markdown")},
        headers={"X-Tenant-Id": TENANT},
    )

    assert response.status_code == 202
    doc_id = response.json()["doc_id"]
    assert (tmp_path / TENANT / KB / f"{doc_id}.md").is_file()

    assert worker_once(engine, embed_client=FakeEmbed(), storage=storage) == 1

    with engine.begin() as connection:
        status = connection.execute(
            text("SELECT status FROM documents WHERE id = :d AND tenant_id = :t"),
            {"d": doc_id, "t": TENANT},
        ).scalar_one()
    assert status == "ready"

    retriever = HybridRetriever(
        PgVectorStore(engine, tenant_id=TENANT, kb_id=KB),
        BM25Store(engine).load(KB),
        FakeEmbed(),
    )
    hits = retriever.retrieve("七天无理由退款", top_k=5, mode="hybrid")

    assert hits
    assert hits[0].doc_id == doc_id
```

**Step 2: 跑测试确认失败**

```powershell
py -3.13 -m pytest tests/test_storage_e2e.py -q
```

Expected: FAIL，`TypeError: worker_once() got an unexpected keyword argument 'storage'`（或上传仍写死 `data/uploads` 导致文件不在 `tmp_path`）。

**Step 3: 让 `ingest.py` 接受内存内容**

Before（`_extract_text` 第 39–44 行）：

```python
def _extract_text(file_path: Path, parser: Any) -> str:
    """按后缀提取纯文本/Markdown；不支持的格式直接抛 ValueError。"""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        content = file_path.read_bytes()
```

After：

```python
def _extract_text(
    file_path: Path, parser: Any, content: bytes | None = None
) -> str:
    """按后缀提取纯文本/Markdown；不支持的格式直接抛 ValueError。

    `content` 用于从对象存储直接取回的内存字节，避免 worker 回写临时文件。
    """
    suffix = file_path.suffix.lower()
    data = content if content is not None else file_path.read_bytes()
    if suffix == ".pdf":
```

PDF 分支内把 `client.parse(content, ...)` 两处改为 `client.parse(data, ...)`；办公分支 `_OFFICE_PARSERS[suffix](data)`；文本分支 `data.decode("utf-8")`。

`ingest_document` 签名 Before：

```python
    path: str | Path,
    embed_client: Any,
```

After：

```python
    path: str | Path,
    embed_client: Any,
    content: bytes | None = None,
```

函数体内 Before：

```python
    text_content = _extract_text(file_path, parser)
```

After：

```python
    text_content = _extract_text(file_path, parser, content)
```

**Step 4: 改造 `src/worker.py`**

导入区 Before：

```python
import os
import sys
from pathlib import Path
from typing import Any
...
from .ingest_jobs import (
    claim_next,
    get_job,
    mark_failed,
    mark_progress,
    mark_succeeded,
)

UPLOAD_ROOT = Path("data/uploads")
```

After：

```python
import os
import sys
from typing import Any
...
from .ingest_jobs import (
    claim_next,
    get_job,
    mark_failed,
    mark_progress,
    mark_succeeded,
)
from .storage import Storage, build_storage
```

`worker_once` 签名 Before：

```python
def worker_once(
    engine: Engine,
    *,
    embed_client: Any,
    parser: Any = None,
    worker_id: str | None = None,
) -> int:
```

After：

```python
def worker_once(
    engine: Engine,
    *,
    embed_client: Any,
    parser: Any = None,
    worker_id: str | None = None,
    storage: Storage | None = None,
) -> int:
```

取文件 Before（第 92–102 行）：

```python
        path = UPLOAD_ROOT / source_url
        result = ingest_document(
            engine,
            tenant_id=job["tenant_id"],
            kb_id=job["kb_id"],
            doc_id=job["doc_id"],
            path=path,
            embed_client=embed_client,
            parser=parser,
            on_stage=on_stage,
        )
```

After：

```python
        active_storage = storage if storage is not None else build_storage()
        if not active_storage.exists(source_url):
            # 文件缺失重传也不会自己出现，按不可重试失败处理，避免空跑三次
            raise NonRetryableIngestError(
                f"上传文件不存在：{source_url}，请重新上传"
            )
        result = ingest_document(
            engine,
            tenant_id=job["tenant_id"],
            kb_id=job["kb_id"],
            doc_id=job["doc_id"],
            path=source_url,
            content=active_storage.get_bytes(source_url),
            embed_client=embed_client,
            parser=parser,
            on_stage=on_stage,
        )
```

**Step 5: 改造 `app/routers/documents.py`**

导入区 Before：

```python
from src import ingest_jobs
from src.ingest_events import record_job_event
```

After：

```python
from src import ingest_jobs
from src.ingest_events import record_job_event
from src.storage import StorageError, build_storage
```

删除第 36 行 `UPLOAD_ROOT = Path("data/uploads")`（`Path` 仍用于后缀与 key，保留导入）。

写入 Before（第 134–149 行）：

```python
    # 数据库已提交，再落盘：这里的失败只会留下 failed 行，不会留下 pending 行
    relative_path = Path(tenant_id) / kb_id / f"{doc_id}{suffix}"
    destination = UPLOAD_ROOT / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    except OSError as exc:
```

After：

```python
    # 数据库已提交，再写存储：这里的失败只会留下 failed 行，不会留下 pending 行
    key = (Path(tenant_id) / kb_id / f"{doc_id}{suffix}").as_posix()
    try:
        build_storage().put_bytes(key, content)
    except (OSError, StorageError) as exc:
```

**Step 6: 改造 `src/ingest_jobs.py`**

导入区 Before：

```python
from sqlalchemy import text
from sqlalchemy.engine import Engine

STATUSES = ("queued", "running", "succeeded", "failed")
MAX_ATTEMPTS = 3
ALLOWED_SOURCE_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}
UPLOAD_ROOT = Path("data/uploads")
```

After：

```python
from sqlalchemy import text
from sqlalchemy.engine import Engine

from .storage import LocalStorage, Storage, build_storage

STATUSES = ("queued", "running", "succeeded", "failed")
MAX_ATTEMPTS = 3
ALLOWED_SOURCE_SUFFIXES = {".pdf", ".md", ".txt", ".docx", ".pptx", ".xlsx"}
```

`receive_source_document` 签名 Before：

```python
    content: bytes,
    upload_root: str | Path | None = None,
) -> dict:
```

After：

```python
    content: bytes,
    upload_root: str | Path | None = None,
    storage: Storage | None = None,
) -> dict:
```

写入 Before（第 148–158 行）：

```python
    destination = (Path(upload_root) if upload_root is not None else UPLOAD_ROOT) / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination.with_name(
            f".{destination.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_bytes(content)
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        job_id = enqueue(engine, tenant_id, kb_id, doc_id, source=source_name)
```

After：

```python
    if storage is not None:
        resolved_storage: Storage = storage
    elif upload_root is not None:
        # upload_root 仅保留给测试与本地脚本显式指定目录，生产走 build_storage()
        resolved_storage = LocalStorage(upload_root)
    else:
        resolved_storage = build_storage()

    try:
        resolved_storage.put_bytes(relative_path.as_posix(), content)
        job_id = enqueue(engine, tenant_id, kb_id, doc_id, source=source_name)
```

其后的 `except Exception:` 回滚逻辑保持不变。

**Step 7: 更新受影响的既有测试**

`tests/test_ingest_jobs.py` 导入区补：

```python
from src.storage import LocalStorage
```

把 5 处：

```python
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
```

统一替换为（`replace_all`）：

```python
    monkeypatch.setattr("src.worker.build_storage", lambda: LocalStorage(tmp_path))
```

`tests/test_worker_connectors.py` 导入区补 `from src.storage import LocalStorage`，第 192 行同样替换：

```python
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)
```

→

```python
    monkeypatch.setattr("src.worker.build_storage", lambda: LocalStorage(tmp_path))
```

`tests/test_ingest_jobs.py` 第 299–319 行的缺失文件测试，按 spec「文件缺失按不可重试错误处理」改为一次即失败。Before：

```python
def test_worker_marks_document_failed_after_max_retries(
    engine, tmp_path, monkeypatch
) -> None:
    source_url = f"{TENANT}/{KB}/missing.md"
    _seed_document(engine, "d2", source_url, "worker_doc_hash_2")
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    monkeypatch.setattr("src.worker.UPLOAD_ROOT", tmp_path)

    for _ in range(3):
        assert worker_once(engine, embed_client=FakeEmbed()) == 1

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["finished_at"] is not None
    assert job["error"]
```

After：

```python
def test_worker_fails_missing_file_without_retry(
    engine, tmp_path, monkeypatch
) -> None:
    source_url = f"{TENANT}/{KB}/missing.md"
    _seed_document(engine, "d2", source_url, "worker_doc_hash_2")
    job_id = enqueue(engine, TENANT, KB, doc_id="d2")
    monkeypatch.setattr("src.worker.build_storage", lambda: LocalStorage(tmp_path))

    assert worker_once(engine, embed_client=FakeEmbed()) == 1
    assert worker_once(engine, embed_client=FakeEmbed()) == 0

    job = get_job(engine, TENANT, job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    assert "上传文件不存在" in job["error"]
    assert job["finished_at"] is not None
```

**补充约定：区分「文件不存在」与「存储故障」**

- `FileNotFoundError`、S3 `NoSuchKey`/`404` → `NonRetryableIngestError`，1 次即失败
- 其余存储异常（连接失败、超时、5xx）→ 保持可重试，沿用最多 3 次
- 新增测试 `test_worker_retries_transient_storage_error`：`build_storage()` 返回的 storage 在 `get_bytes` 抛 `ConnectionError`，断言前两次 `worker_once` 后任务仍为 `pending`/`running` 且 `attempts` 递增，第 3 次后 `failed`

**Step 8: 跑新增与受影响测试确认通过**

```powershell
py -3.13 -m pytest tests/test_storage_e2e.py tests/test_storage.py tests/test_ingest_jobs.py tests/test_worker_connectors.py tests/test_api_documents.py -q
```

Expected: 全部 passed（数据库不可用时相关用例 skip），无 failed。

**Step 9: 提交**

```powershell
git add src/storage.py src/ingest.py src/worker.py src/ingest_jobs.py app/routers/documents.py tests/test_storage_e2e.py tests/test_ingest_jobs.py tests/test_worker_connectors.py
git commit -m "refactor(ingest): read uploads through storage abstraction"
```

---

### Task 4: Compose 增加 MinIO 与建桶初始化

**Files:**
- Modify: `deploy/docker-compose.yml`（api 环境第 29–38 行、worker 环境第 60–68 行、volumes 第 96–98 行）

**Step 1: 写验证命令（无单测，使用配置校验）**

Run:

```powershell
docker compose -f deploy/docker-compose.yml config --services
```

Expected（改动前）：`db`、`api`、`worker`、`parser`。改动后应包含 `minio`、`minio-init`。

**Step 2: 在 `services` 下新增 MinIO 服务**

在 `parser` 服务之后、顶层 `volumes:` 之前追加：

```yaml
  # 本地对象存储：仅 STORAGE_BACKEND=s3 时需要。API 9000、控制台 9001。
  minio:
    image: docker.m.daocloud.io/minio/minio:latest
    container_name: rag-kb-minio
    command: ["server", "/data", "--console-address", ":9001"]
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:-ragkb}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-ragkb-secret}
    ports:
      - "9000:9000"
      - "9001:9001"
    volumes:
      - miniodata:/data
    restart: unless-stopped

  # 一次性建桶；minio 可能尚未就绪，用 alias 重试直到成功。
  minio-init:
    image: docker.m.daocloud.io/minio/mc:latest
    container_name: rag-kb-minio-init
    depends_on:
      minio:
        condition: service_started
    entrypoint:
      - /bin/sh
      - -c
      - >
        until mc alias set local http://minio:9000 "$$MINIO_ROOT_USER" "$$MINIO_ROOT_PASSWORD"; do sleep 2; done;
        mc mb --ignore-existing local/rag-kb;
        mc ls local/rag-kb;
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:-ragkb}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-ragkb-secret}
    restart: "no"
```

**Step 3: 给 api 注入 S3 环境变量**

`api.environment` Before 末尾：

```yaml
      # 没有 EMBED_API_KEY 时用 1024 维哈希向量，保证链路可用
      EMBED_MODE: mock
```

After：

```yaml
      # 没有 EMBED_API_KEY 时用 1024 维哈希向量，保证链路可用
      EMBED_MODE: mock
      # 容器内必须用服务名 minio；生产把 S3_ENDPOINT 改成实际地址
      STORAGE_BACKEND: ${STORAGE_BACKEND:-local}
      S3_ENDPOINT: http://minio:9000
      S3_BUCKET: ${S3_BUCKET:-rag-kb}
      S3_ACCESS_KEY: ${S3_ACCESS_KEY:-ragkb}
      S3_SECRET_KEY: ${S3_SECRET_KEY:-ragkb-secret}
      S3_REGION: ${S3_REGION:-us-east-1}
      S3_USE_SSL: ${S3_USE_SSL:-false}
```

**Step 4: 给 worker 注入同一组变量**

`worker.environment` Before：

```yaml
      EMBED_MODE: mock
      PARSER_BASE_URL: http://parser:8000
```

After：

```yaml
      EMBED_MODE: mock
      PARSER_BASE_URL: http://parser:8000
      STORAGE_BACKEND: ${STORAGE_BACKEND:-local}
      S3_ENDPOINT: http://minio:9000
      S3_BUCKET: ${S3_BUCKET:-rag-kb}
      S3_ACCESS_KEY: ${S3_ACCESS_KEY:-ragkb}
      S3_SECRET_KEY: ${S3_SECRET_KEY:-ragkb-secret}
      S3_REGION: ${S3_REGION:-us-east-1}
      S3_USE_SSL: ${S3_USE_SSL:-false}
```

**Step 5: 保留 uploads 卷并新增 miniodata**

顶层 Before：

```yaml
volumes:
  pgdata: {}
  uploads: {}
```

After：

```yaml
volumes:
  pgdata: {}
  # local 模式仍需要 api 与 worker 共享；s3 模式可不再依赖它
  uploads: {}
  miniodata: {}
```

**Step 6: 校验配置**

```powershell
docker compose -f deploy/docker-compose.yml config --services
```

Expected: 输出包含 `minio` 与 `minio-init`，且 `config` 无 YAML 报错。

**Step 7: Commit**

```powershell
git add deploy/docker-compose.yml
git commit -m "feat(deploy): add minio and bucket initialization"
```

---

### Task 5: `.env.example` 增加存储配置

**Files:**
- Modify: `.env.example`（第 14–16 行之后）

**Step 1: 追加配置块**

After：

```dotenv
# PDF 解析服务（留空 = 用本地 pypdf 降级；起容器时填 http://localhost:8100）
PARSER_BASE_URL=

# 原始文件存储：local（默认，写 data/uploads）| s3（MinIO / 云对象存储）
# 宿主机直跑填 http://127.0.0.1:9000；compose 内固定走服务名 http://minio:9000
STORAGE_BACKEND=local
S3_ENDPOINT=http://127.0.0.1:9000
S3_BUCKET=rag-kb
S3_ACCESS_KEY=ragkb
S3_SECRET_KEY=ragkb-secret
S3_REGION=us-east-1
S3_USE_SSL=false
```

**Step 2: 验证不污染代码路径**

```powershell
py -3.13 -c "import os; os.environ['STORAGE_BACKEND']='local'; from src.storage import build_storage; print(type(build_storage()).__name__)"
```

Expected: `LocalStorage`。

**Step 3: Commit**

```powershell
git add .env.example
git commit -m "docs(config): document storage backend settings"
```

---

### Task 6: README 与文档更新

**Files:**
- Modify: `README.md`（第 12 行图、第 47–48 行边界说明、第 56 行前置条件、第 88 行之后追加对象存储小节）

**Step 1: 更新架构图中的存储节点**

Before（第 12 行）：

```markdown
    S[(共享 data/uploads<br/>named volume)]
```

After：

```markdown
    S[(原始文件<br/>local / MinIO S3)]
```

**Step 2: 更新入库边界说明**

Before（第 47 行）：

```markdown
- **入库边界**：上传 API 只负责接收、去重、落盘和入队；worker 每轮最多处理一条任务，通过 `FOR UPDATE SKIP LOCKED` 原子领取，执行任务状态、进度与最多三次尝试。API 与 worker 必须共享上传目录。
```

After：

```markdown
- **入库边界**：上传 API 只负责接收、去重、写存储和入队；worker 每轮最多处理一条任务，通过 `FOR UPDATE SKIP LOCKED` 原子领取，执行任务状态、进度与最多三次尝试。原始文件默认写本地 `data/uploads`（`STORAGE_BACKEND=local`，API 与 worker 必须共享该卷）；设置 `STORAGE_BACKEND=s3` 后改走 MinIO 或云对象存储，key 布局不变，不再依赖共享卷。
```

**Step 3: 补充办公格式解析说明**

Before（第 48 行）：

```markdown
- **PDF 边界**：独立 parser 使用 pdfplumber 提取标题、文本和表格块，只有解析块列表为空才调用 RapidOCR；未配置 parser 或 parser 请求失败时，主服务退回本地 pypdf，本地路径不会自行 OCR。
```

After：

```markdown
- **PDF 边界**：独立 parser 使用 pdfplumber 提取标题、文本和表格块，只有解析块列表为空才调用 RapidOCR；未配置 parser 或 parser 请求失败时，主服务退回本地 pypdf，本地路径不会自行 OCR。
- **办公格式边界**：`.docx` / `.pptx` / `.xlsx` 由 python-docx / python-pptx / openpyxl 在进程内直接转 Markdown，不经过 parser 容器；纯图片 PPT 建议先转 PDF 再走 OCR。
```

**Step 4: 更新前置条件描述**

Before（第 56 行首句）：

```markdown
前置条件：Windows PowerShell，以及带 Docker Compose 的 Docker Desktop。以下五条命令均在仓库根目录顺序执行。已提交的 Compose 只定义 `db/api/worker`；扫描件 OCR 所需的 parser 使用已提交的 `parser/Dockerfile` 单独构建和运行，不依赖任何 Compose profile。
```

After：

```markdown
前置条件：Windows PowerShell，以及带 Docker Compose 的 Docker Desktop。以下五条命令均在仓库根目录顺序执行。已提交的 Compose 定义 `db/api/worker`，以及可选的 `minio/minio-init`；扫描件 OCR 所需的 parser 使用已提交的 `parser/Dockerfile` 单独构建和运行，不依赖任何 Compose profile。
```

**Step 5: 追加对象存储小节**

在第 88 行 parser 说明段之后插入：

````markdown
### 可选：MinIO 对象存储

默认 `STORAGE_BACKEND=local`，行为与旧版本一致。需要验证对象存储时：

```powershell
docker compose -f deploy/docker-compose.yml up -d minio minio-init
```

`minio-init` 是一次性容器，负责创建 `rag-kb` bucket，日志出现 `rag-kb` 列表即成功。随后把环境变量设为 `STORAGE_BACKEND=s3` 并重启 api/worker（compose 内 `S3_ENDPOINT` 固定为 `http://minio:9000`，控制台在 `http://127.0.0.1:9001`）。回滚只需把 `STORAGE_BACKEND` 改回 `local`，key 布局与本地路径一一对应，无需迁移数据。
````

**Step 6: 校验 Markdown 结构**

```powershell
py -3.13 -c "from pathlib import Path; text=Path('README.md').read_text(encoding='utf-8'); assert '### 可选：MinIO 对象存储' in text and 'STORAGE_BACKEND=s3' in text; print('README OK')"
```

Expected: `README OK`。

**Step 7: Commit**

```powershell
git add README.md
git commit -m "docs(readme): document office formats and object storage"
```

---

### Task 7: 全量回归与 spec §7 验收

**Files:** 无新增/修改，只验证。

**Step 1: Python 全量回归**

```powershell
py -3.13 -m pytest -q
```

Expected: 无 `failed`；`test_parsers_office.py`、`test_storage.py`、`test_storage_e2e.py` 均在收集列表内。

**Step 2: Node 全量回归**

```powershell
node --test tests/js/core.test.mjs tests/js/overview.test.mjs tests/js/lab.test.mjs tests/js/traces.test.mjs tests/js/i18n.test.mjs
```

Expected: `# fail 0`。

**Step 3: 验收 §7-1：三格式上传 202 且任务 succeeded**

```powershell
py -3.13 -m pytest tests/test_api_documents.py::test_upload_accepts_office_formats tests/test_ingest.py::test_ingest_docx_end_to_end -q
```

Expected: 全部 passed。手工路径：UI `http://127.0.0.1:8000/ui/` 分别上传 docx/pptx/xlsx，任务面板最终显示 `succeeded`。

**Step 4: 验收 §7-2、§7-3：Markdown 结构与 xlsx 多 sheet/截断**

```powershell
py -3.13 -m pytest tests/test_parsers_office.py -q
```

Expected: `11 passed`；覆盖标题层级、表格转 Markdown、多 sheet、空文档、合并单元格、5000 行截断提示。

**Step 5: 验收 §7-4：S3 后端上传可见、worker 可入库**

```powershell
docker compose -f deploy/docker-compose.yml up -d minio minio-init
docker compose -f deploy/docker-compose.yml logs minio-init
```

Expected: `minio-init` 退出码 0，日志包含 `rag-kb`。随后设置 `STORAGE_BACKEND=s3` 重启 `api/worker`，上传一个 `.md`，在 MinIO 控制台 `http://127.0.0.1:9001` 的 `rag-kb` bucket 下应看到 `{tenant}/{kb}/{doc_id}.md`，任务变为 `succeeded`。

**Step 6: 验收 §7-5：local 行为与测试不变**

```powershell
$env:STORAGE_BACKEND="local"
py -3.13 -m pytest tests/test_api_documents.py tests/test_ingest_jobs.py tests/test_worker_connectors.py -q
```

Expected: 全部 passed，`data/uploads` 下仍按 `{tenant}/{kb}/{doc_id}{suffix}` 落盘。

**Step 7: 最终检查**

```powershell
git status --short
```

Expected: 只剩 `.env`、`data/`、`__pycache__` 之外无意外文件；无未提交的源码改动。
