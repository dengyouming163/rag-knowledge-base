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
