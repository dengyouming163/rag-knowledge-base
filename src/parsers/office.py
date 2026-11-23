"""办公文档解析：把 .docx / .pptx / .xlsx 转成 Markdown。

只提取文本与表格，不处理图片、图表和形状内文字。无法打开的文件统一抛
OfficeParseError，由 ingest._extract_text 转成不可重试的入库错误，避免
加密/损坏文件白白消耗三次重试。
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from src.tables import rows_to_markdown

MAX_SHEET_ROWS = 5000
TRUNCATION_NOTE = "已截断，仅索引前 5000 行"

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
